#!/usr/bin/env python3
"""
OCI VM Cloner

Prompts for OCI credentials, reads a source VM configuration,
generates Terraform files for a clone, and optionally runs Terraform
with confirmation prompts at each stage.
"""

from __future__ import annotations

import datetime as dt
import getpass
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import oci


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def setup_logging(base_dir: Path) -> Tuple[logging.Logger, Path]:
    log_dir = base_dir / f"logs_{now_stamp()}"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("oci_vm_cloner")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(log_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger, log_dir


def ask(prompt: str, required: bool = True, default: Optional[str] = None, secret: bool = False) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        text = f"{prompt}{suffix}: "
        value = getpass.getpass(text) if secret else input(text)
        value = value.strip()
        if not value and default is not None:
            value = default
        if value or not required:
            return value
        print("Value is required.")


def ask_yes_no(prompt: str, default_no: bool = True) -> bool:
    suffix = " [y/N]: " if default_no else " [Y/n]: "
    ans = input(prompt + suffix).strip().lower()
    if not ans:
        return not default_no
    return ans in {"y", "yes"}


def sanitize_name(value: str) -> str:
    clean = "".join(c if c.isalnum() or c in "-_" else "_" for c in value)
    return clean or "target"


def terraform_bool_str(value: bool) -> str:
    return "true" if value else "false"


def load_oci_config(logger: logging.Logger) -> Dict[str, Any]:
    logger.info("Collecting OCI authentication details.")
    auth_mode = ask(
        "Select auth mode: 1=Use OCI config file (~/.oci/config), 2=Manual API key input",
        default="1",
    )

    if auth_mode == "1":
        config_path = ask("OCI config file path", default=str(Path.home() / ".oci" / "config"))
        profile = ask("OCI profile", default="DEFAULT")
        config = oci.config.from_file(config_path, profile_name=profile)
    else:
        config = {
            "tenancy": ask("Tenancy OCID"),
            "user": ask("User OCID"),
            "fingerprint": ask("API key fingerprint"),
            "key_file": ask("API private key file path"),
            "pass_phrase": ask("Private key passphrase (optional)", required=False),
            "region": ask("OCI region (e.g., us-ashburn-1)"),
        }

    oci.config.validate_config(config)
    return config


def test_login(config: Dict[str, Any], logger: logging.Logger) -> None:
    logger.info("Testing OCI login with subscription-manager equivalent identity call.")
    identity_client = oci.identity.IdentityClient(config)
    user_ocid = config.get("user")
    if not user_ocid:
        raise RuntimeError("Missing user OCID in OCI configuration.")

    user = identity_client.get_user(user_ocid).data
    logger.info("Login test successful. Authenticated OCI user: %s", user.name)


def find_source_instance(
    config: Dict[str, Any],
    compartment_ocid: str,
    instance_name: str,
    instance_ip: str,
    logger: logging.Logger,
) -> Dict[str, Any]:
    compute = oci.core.ComputeClient(config)
    network = oci.core.VirtualNetworkClient(config)

    logger.info("Looking up source instance '%s' in compartment %s", instance_name, compartment_ocid)
    instances = oci.pagination.list_call_get_all_results(
        compute.list_instances,
        compartment_id=compartment_ocid,
        display_name=instance_name,
    ).data

    if not instances:
        raise RuntimeError(f"No instance found with name '{instance_name}' in source compartment.")

    if len(instances) > 1:
        logger.warning("Multiple instances found with the same name. Using the first result.")

    instance = instances[0]

    attachments = oci.pagination.list_call_get_all_results(
        compute.list_vnic_attachments,
        compartment_id=compartment_ocid,
        instance_id=instance.id,
    ).data
    if not attachments:
        raise RuntimeError("No VNIC attachment found for source instance.")

    vnic = network.get_vnic(attachments[0].vnic_id).data

    candidate_ips = [vnic.private_ip]
    if vnic.public_ip:
        candidate_ips.append(vnic.public_ip)

    if instance_ip not in candidate_ips:
        raise RuntimeError(
            "Source instance IP mismatch. "
            f"Provided: {instance_ip}; Found: {', '.join(ip for ip in candidate_ips if ip)}"
        )

    source = {
        "instance": instance,
        "vnic": vnic,
    }

    logger.info(
        "Source instance located: id=%s shape=%s AD=%s private_ip=%s public_ip=%s",
        instance.id,
        instance.shape,
        instance.availability_domain,
        vnic.private_ip,
        vnic.public_ip,
    )
    return source


def generate_terraform_files(
    output_dir: Path,
    config: Dict[str, Any],
    source: Dict[str, Any],
    target: Dict[str, str],
    logger: logging.Logger,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    instance = source["instance"]
    vnic = source["vnic"]

    source_ocpus = 0
    source_memory = 0
    if instance.shape_config is not None:
        source_ocpus = instance.shape_config.ocpus or 0
        source_memory = instance.shape_config.memory_in_gbs or 0

    assign_public_ip = bool(vnic.public_ip)
    nsg_ids = vnic.nsg_ids or []

    variables_tf = """
variable "tenancy_ocid" { type = string }
variable "user_ocid" { type = string }
variable "fingerprint" { type = string }
variable "private_key_path" { type = string }
variable "private_key_password" { type = string }
variable "region" { type = string }

variable "target_compartment_ocid" { type = string }
variable "target_instance_name" { type = string }
variable "target_fqdn" { type = string }
variable "target_image_ocid" { type = string }
variable "admin_username" { type = string }
variable "admin_public_key" { type = string }

variable "source_availability_domain" { type = string }
variable "source_shape" { type = string }
variable "source_subnet_ocid" { type = string }
variable "source_nsg_ids" { type = list(string) }
variable "assign_public_ip" { type = bool }
variable "source_ocpus" { type = number }
variable "source_memory_in_gbs" { type = number }
""".strip() + "\n"

    main_tf = """
terraform {
  required_version = ">= 1.3.0"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 5.0.0"
    }
  }
}

provider "oci" {
  tenancy_ocid         = var.tenancy_ocid
  user_ocid            = var.user_ocid
  fingerprint          = var.fingerprint
  private_key_path     = var.private_key_path
  private_key_password = var.private_key_password != "" ? var.private_key_password : null
  region               = var.region
}

resource "oci_core_instance" "vm_clone" {
  availability_domain = var.source_availability_domain
  compartment_id      = var.target_compartment_ocid
  display_name        = var.target_instance_name
  shape               = var.source_shape

  dynamic "shape_config" {
    for_each = var.source_ocpus > 0 && var.source_memory_in_gbs > 0 ? [1] : []
    content {
      ocpus         = var.source_ocpus
      memory_in_gbs = var.source_memory_in_gbs
    }
  }

  source_details {
    source_type = "image"
    source_id   = var.target_image_ocid
  }

  create_vnic_details {
    subnet_id        = var.source_subnet_ocid
    nsg_ids          = var.source_nsg_ids
    assign_public_ip = var.assign_public_ip
    hostname_label   = replace(lower(var.target_instance_name), "_", "-")
  }

  metadata = {
    ssh_authorized_keys = var.admin_public_key
    user_data = base64encode(templatefile("${path.module}/cloud-init.tftpl", {
      target_fqdn    = var.target_fqdn
      admin_username = var.admin_username
      admin_pub_key  = var.admin_public_key
    }))
  }
}
""".strip() + "\n"

    cloud_init = """
#cloud-config
fqdn: ${target_fqdn}
users:
  - default
  - name: ${admin_username}
    shell: /bin/bash
    groups: [wheel]
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - ${admin_pub_key}
ssh_pwauth: false
""".strip() + "\n"

    tfvars: Dict[str, Any] = {
        "tenancy_ocid": config.get("tenancy", ""),
        "user_ocid": config.get("user", ""),
        "fingerprint": config.get("fingerprint", ""),
        "private_key_path": config.get("key_file", ""),
        "private_key_password": config.get("pass_phrase", "") or "",
        "region": config.get("region", ""),
        "target_compartment_ocid": target["target_compartment_ocid"],
        "target_instance_name": target["target_instance_name"],
        "target_fqdn": target["target_fqdn"],
        "target_image_ocid": target["target_image_ocid"],
        "admin_username": target["admin_username"],
        "admin_public_key": target["admin_public_key"],
        "source_availability_domain": instance.availability_domain,
        "source_shape": instance.shape,
        "source_subnet_ocid": vnic.subnet_id,
        "source_nsg_ids": nsg_ids,
        "assign_public_ip": assign_public_ip,
        "source_ocpus": source_ocpus,
        "source_memory_in_gbs": source_memory,
    }

    source_summary = {
        "instance_id": instance.id,
        "display_name": instance.display_name,
        "compartment_id": instance.compartment_id,
        "shape": instance.shape,
        "availability_domain": instance.availability_domain,
        "fault_domain": instance.fault_domain,
        "subnet_id": vnic.subnet_id,
        "private_ip": vnic.private_ip,
        "public_ip": vnic.public_ip,
        "nsg_ids": nsg_ids,
        "shape_config": {
            "ocpus": source_ocpus,
            "memory_in_gbs": source_memory,
        },
    }

    (output_dir / "variables.tf").write_text(variables_tf, encoding="utf-8")
    (output_dir / "main.tf").write_text(main_tf, encoding="utf-8")
    (output_dir / "cloud-init.tftpl").write_text(cloud_init, encoding="utf-8")
    (output_dir / "terraform.tfvars.json").write_text(json.dumps(tfvars, indent=2), encoding="utf-8")
    (output_dir / "source_instance_config.json").write_text(json.dumps(source_summary, indent=2), encoding="utf-8")

    logger.info("Terraform files generated in %s", output_dir)


def run_command(command: List[str], cwd: Path, log_file: Path, logger: logging.Logger) -> int:
    logger.info("Running command: %s", " ".join(command))
    with log_file.open("w", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            shell=False,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            lf.write(line)
        proc.wait()
        rc = proc.returncode
    logger.info("Command exited with code %s. Log: %s", rc, log_file)
    return rc


def run_terraform(terraform_dir: Path, log_dir: Path, logger: logging.Logger) -> None:
    terraform_bin = shutil.which("terraform")
    if not terraform_bin:
        raise RuntimeError("Terraform binary not found in PATH.")

    stages = [
        ([terraform_bin, "init"], "terraform_init.log"),
        ([terraform_bin, "plan", "-out", "tfplan"], "terraform_plan.log"),
        ([terraform_bin, "apply", "tfplan"], "terraform_apply.log"),
    ]

    for command, log_name in stages:
        if not ask_yes_no(f"Proceed with stage: {' '.join(command)}?", default_no=True):
            logger.info("Stage skipped by user: %s", " ".join(command))
            return
        rc = run_command(command, terraform_dir, log_dir / log_name, logger)
        if rc != 0:
            raise RuntimeError(f"Terraform stage failed ({' '.join(command)}). See {log_dir / log_name}")


def collect_target_details() -> Dict[str, str]:
    print("\nEnter target clone details:")
    target = {
        "target_compartment_ocid": ask("Target Compartment OCID"),
        "target_instance_name": ask("Target Instance Name"),
        "target_fqdn": ask("Target FQDN"),
        "target_image_ocid": ask("Target OS Image OCID (Oracle Linux 8.10 image OCID)"),
        "admin_username": ask("Admin user ID (passwordless sudo)"),
        "admin_public_key": ask("Public key of admin user"),
    }
    return target


def main() -> int:
    base_dir = Path.cwd()
    logger, log_dir = setup_logging(base_dir)

    logger.info("OCI VM Cloner started")
    logger.info("Logs directory: %s", log_dir)

    try:
        config = load_oci_config(logger)
        test_login(config, logger)

        print("\nEnter source VM details:")
        source_compartment_ocid = ask("Source Compartment OCID")
        source_instance_name = ask("Source Instance Name")
        source_instance_ip = ask("Source Instance IP address")

        source = find_source_instance(
            config,
            source_compartment_ocid,
            source_instance_name,
            source_instance_ip,
            logger,
        )

        target = collect_target_details()

        tf_dir = base_dir / f"terraform_clone_{sanitize_name(target['target_instance_name'])}"
        generate_terraform_files(tf_dir, config, source, target, logger)

        if ask_yes_no("Do you want to run Terraform now?", default_no=True):
            run_terraform(tf_dir, log_dir, logger)
            logger.info("Terraform completed successfully.")
        else:
            logger.info("Terraform run skipped by user. Files remain in %s", tf_dir)

        logger.info("Completed successfully.")
        return 0

    except Exception as exc:
        logger.exception("Execution failed: %s", exc)
        print(f"ERROR: {exc}")
        print(f"Check logs in: {log_dir}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
