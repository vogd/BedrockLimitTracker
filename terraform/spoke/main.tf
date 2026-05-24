variable "sink_arn" {
  description = "OAM Sink ARN from monitoring account"
  type        = string
}

variable "monitoring_account_id" {
  description = "Account ID of the monitoring account"
  type        = string
}

variable "is_monitoring_account" {
  description = "Set true if deploying in the monitoring account itself (skips OAM Link)"
  type        = bool
  default     = false
}

# ============ OAM Link ============
resource "aws_oam_link" "this" {
  count            = var.is_monitoring_account ? 0 : 1
  label_template   = "$AccountName"
  resource_types   = ["AWS::CloudWatch::Metric"]
  sink_identifier  = var.sink_arn
  tags             = { Project = "BedrockLimitsTracker" }
}

# ============ Spoke Role ============
resource "aws_iam_role" "spoke" {
  name = "BedrockLimitsTrackerSpokeRole"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.monitoring_account_id}:role/BedrockLimitsTrackerLambdaRole" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "spoke" {
  name = "BedrockQuotaReadPolicy"
  role = aws_iam_role.spoke.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "servicequotas:ListServiceQuotas",
          "servicequotas:GetServiceQuota",
          "servicequotas:ListAWSDefaultServiceQuotas"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "bedrock:ListFoundationModels",
          "bedrock:ListInferenceProfiles",
          "bedrock:ListProvisionedModelThroughputs",
          "bedrock:GetFoundationModelAvailability"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricData",
          "cloudwatch:GetMetricStatistics"
        ]
        Resource = "*"
      }
    ]
  })
}

output "spoke_role_arn" {
  value = aws_iam_role.spoke.arn
}
