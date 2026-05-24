output "sink_arn" {
  description = "OAM Sink ARN — use in spoke module"
  value       = aws_oam_sink.this.arn
}

output "lambda_function_arn" {
  value = aws_lambda_function.this.arn
}

output "monitoring_account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "cache_bucket" {
  value = aws_s3_bucket.cache.id
}
