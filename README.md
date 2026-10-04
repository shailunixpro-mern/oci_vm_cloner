# OCI VM Cloner

This project provides an interactive Python CLI that:

1. Authenticates to OCI
2. Reads a source VM configuration
3. Generates Terraform files for a clone VM
4. Optionally runs Terraform (`init`, `plan`, `apply`) with confirmation prompts
5. Captures run logs and Terraform output logs in a timestamped log directory

## Files

- `oci_vm_cloner.py`: Main interactive script
- `requirements.txt`: Python dependency list
- `terraform_clone_<target_name>/`: Generated Terraform folder (created at runtime)
- `logs_<timestamp>/`: Runtime and Terraform logs (created at runtime)

## Prerequisites

1. Python 3.9+
2. Terraform in `PATH`
3. OCI API credentials (config file or API key details)
4. Network access to OCI APIs

Install dependency:

```bash
pip install -r requirements.txt
```

Run script:

```bash
python oci_vm_cloner.py
```

## Stage-by-Stage Prompts and Expected Outputs

### Stage 0: OCI Authentication and Login Test

Sample prompts:

```text
Select auth mode: 1=Use OCI config file (~/.oci/config), 2=Manual API key input [1]:
OCI config file path [C:\Users\<you>\.oci\config]:
OCI profile [DEFAULT]:
```

Expected output (example):

```text
INFO | Collecting OCI authentication details.
INFO | Testing OCI login with subscription-manager equivalent identity call.
INFO | Login test successful. Authenticated OCI user: john.doe@example.com
```

If auth fails, expected output (example):

```text
ERROR: <OCI auth or config validation error>
Check logs in: .\logs_YYYYMMDD_HHMMSS
```

### Stage 1: Source VM Input and Validation

Sample prompts:

```text
Enter source VM details:
Source Compartment OCID:
Source Instance Name:
Source Instance IP address:
```

Expected success output (example):

```text
INFO | Looking up source instance 'src-app-01' in compartment ocid1.compartment.oc1..aaaa...
INFO | Source instance located: id=ocid1.instance.oc1..aaaa... shape=VM.Standard.E4.Flex AD=Uocm:PHX-AD-1 private_ip=10.0.1.21 public_ip=129.146.x.x
```

Possible validation errors:

- Instance not found by name in source compartment
- No VNIC attachment found
- Source IP mismatch with discovered private/public IP

### Stage 2: Target VM Input

Sample prompts:

```text
Enter target clone details:
Target Compartment OCID:
Target Instance Name:
Target FQDN:
Target OS Image OCID (Oracle Linux 8.10 image OCID):
Admin user ID (passwordless sudo):
Public key of admin user:
```

Expected behavior:

- Script accepts inputs and proceeds to Terraform generation.

### Stage 3: Terraform File Generation

Expected output (example):

```text
INFO | Terraform files generated in C:\path\to\repo\terraform_clone_target-app-01
```

Expected generated files:

- `variables.tf`
- `main.tf`
- `terraform.tfvars.json`
- `cloud-init.tftpl`
- `source_instance_config.json`

Cloud-init behavior in generated template:

- Sets target FQDN
- Creates admin user
- Grants passwordless sudo (`NOPASSWD`)
- Adds provided SSH public key

### Stage 4: Terraform Execution Prompts

Prompt before execution:

```text
Do you want to run Terraform now? [y/N]:
```

If `y`, stage-by-stage prompts:

```text
Proceed with stage: terraform init? [y/N]:
Proceed with stage: terraform plan -out tfplan? [y/N]:
Proceed with stage: terraform apply tfplan? [y/N]:
```

Expected outputs:

- Each command streams output to terminal
- Each stage writes a dedicated log file in the run log directory:
  - `terraform_init.log`
  - `terraform_plan.log`
  - `terraform_apply.log`

If all stages pass, expected output (example):

```text
INFO | Terraform completed successfully.
INFO | Completed successfully.
```

If any stage fails, expected output (example):

```text
ERROR: Terraform stage failed (terraform plan -out tfplan). See ...\terraform_plan.log
Check logs in: .\logs_YYYYMMDD_HHMMSS
```

## Logging Details

For each run, script creates:

- `logs_<timestamp>/run.log`: Script-level logs and major actions
- `logs_<timestamp>/terraform_*.log`: Raw Terraform stage outputs

This log folder is created in the same directory where the script is executed.

## Notes

1. Script reads source VM config from OCI and uses those values to build clone Terraform configuration.
2. Target image OCID must be valid for your selected region and availability domain.
3. The admin user is created via cloud-init with passwordless sudo and provided SSH key.
4. If Terraform is not in `PATH`, the script exits with a clear error.

## Quick Test Flow

1. `pip install -r requirements.txt`
2. `python oci_vm_cloner.py`
3. Provide OCI auth details
4. Provide source VM details
5. Provide target clone details
6. Confirm Terraform stages (`init`, `plan`, `apply`) when prompted
7. Review `logs_<timestamp>` for details and troubleshooting
