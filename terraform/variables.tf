variable "region" {
  description = "AWS region for the monitoring account"
  type        = string
  default     = "us-east-1"
}

variable "source_account_ids" {
  description = "List of AWS account IDs to monitor"
  type        = list(string)
}

variable "bedrock_regions" {
  description = "Regions to scan for Bedrock quotas"
  type        = list(string)
  default     = ["us-east-1", "us-west-2"]
}

variable "organization_id" {
  description = "AWS Organization ID (optional — allows all org accounts to link)"
  type        = string
  default     = ""
}

variable "notification_email" {
  description = "Email for alarm notifications (optional)"
  type        = string
  default     = ""
}
