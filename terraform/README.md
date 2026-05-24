# Terraform Deployment

Alternative to CloudFormation. Deploys the same resources.

## Structure

```
terraform/
├── main.tf                  # Monitoring account resources
├── variables.tf             # Input variables
├── outputs.tf               # Sink ARN, Lambda ARN
├── terraform.tfvars.example # Example config
└── spoke/
    └── main.tf              # Spoke module (OAM Link + IAM Role)
```

## Deploy Monitoring Account

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your account IDs

terraform init
terraform plan
terraform apply
```

## Deploy Spoke Accounts

For each spoke account, use the spoke module with a provider alias:

```hcl
# In a separate workspace or with provider aliases:
provider "aws" {
  alias   = "spoke_a"
  region  = "us-east-1"
  assume_role {
    role_arn = "arn:aws:iam::<SPOKE_ACCOUNT>:role/TerraformRole"
  }
}

module "spoke_a" {
  source                = "./spoke"
  providers             = { aws = aws.spoke_a }
  sink_arn              = module.monitoring.sink_arn
  monitoring_account_id = module.monitoring.monitoring_account_id
}

# For the monitoring account itself:
module "spoke_self" {
  source                = "./spoke"
  sink_arn              = module.monitoring.sink_arn
  monitoring_account_id = module.monitoring.monitoring_account_id
  is_monitoring_account = true
}
```

## Notes

- The Lambda code is referenced from `../lambda/index.py` (shared with CloudFormation deployment)
- S3 bucket name: `bedrock-limits-cache-<account_id>` (auto-generated)
- OAM Link is skipped when `is_monitoring_account = true`
