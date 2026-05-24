terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

# ============ OAM Sink ============
resource "aws_oam_sink" "this" {
  name = "BedrockLimitsTrackerSink"
  tags = { Project = "BedrockLimitsTracker" }
}

resource "aws_oam_sink_policy" "this" {
  sink_identifier = aws_oam_sink.this.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = var.organization_id != "" ? "*" : { AWS = var.source_account_ids }
      Resource  = "*"
      Action    = ["oam:CreateLink", "oam:UpdateLink"]
      Condition = merge(
        var.organization_id != "" ? { StringEquals = { "aws:PrincipalOrgID" = var.organization_id } } : {},
        { "ForAllValues:StringEquals" = { "oam:ResourceTypes" = ["AWS::CloudWatch::Metric"] } }
      )
    }]
  })
}

# ============ S3 Cache Bucket ============
resource "aws_s3_bucket" "cache" {
  bucket = "bedrock-limits-cache-${data.aws_caller_identity.current.account_id}"
  tags   = { Project = "BedrockLimitsTracker" }
}

# ============ SSM Parameter ============
resource "aws_ssm_parameter" "accounts" {
  name  = "/BedrockLimitsTracker/SourceAccounts"
  type  = "StringList"
  value = join(",", var.source_account_ids)
}

# ============ Lambda Role ============
resource "aws_iam_role" "lambda" {
  name = "BedrockLimitsTrackerLambdaRole"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda" {
  name = "BedrockLimitsTrackerPolicy"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = "arn:aws:iam::*:role/BedrockLimitsTrackerSpokeRole"
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData", "cloudwatch:PutDashboard"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/BedrockLimitsTracker/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.cache.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.cache.arn
      }
    ]
  })
}

# ============ Lambda Function ============
data "archive_file" "lambda" {
  type        = "zip"
  source_file = "${path.module}/../lambda/index.py"
  output_path = "${path.module}/.build/lambda.zip"
}

resource "aws_lambda_function" "this" {
  function_name    = "bedrock_LimitsTracker"
  role             = aws_iam_role.lambda.arn
  handler          = "index.handler"
  runtime          = "python3.12"
  timeout          = 600
  memory_size      = 256
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      SOURCE_ACCOUNTS_PARAM = aws_ssm_parameter.accounts.name
      BEDROCK_REGIONS       = join(",", var.bedrock_regions)
      CACHE_BUCKET          = aws_s3_bucket.cache.id
    }
  }
}

# ============ EventBridge Schedule ============
resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "BedrockLimitsTrackerSchedule"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = aws_lambda_function.this.arn
}

resource "aws_lambda_permission" "eventbridge" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}

# ============ CloudWatch Alarms ============
resource "aws_cloudwatch_metric_alarm" "red" {
  alarm_name          = "Bedrock-Limits-RED-Critical"
  alarm_description   = "One or more Bedrock models exceed 90% quota utilization"
  namespace           = "BedrockLimits"
  metric_name         = "ProfilesOver75Pct_RPM"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.notification_email != "" ? [aws_sns_topic.alerts[0].arn] : []
}

# ============ SNS (optional) ============
resource "aws_sns_topic" "alerts" {
  count = var.notification_email != "" ? 1 : 0
  name  = "BedrockLimitsAlarms"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.notification_email
}
