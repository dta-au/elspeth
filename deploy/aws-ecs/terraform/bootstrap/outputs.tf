output "backend_state_bucket" {
  value = aws_s3_bucket.terraform_state.id
}

output "ecr_repository" {
  value = aws_ecr_repository.acceptance.name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.acceptance.repository_url
}

output "cloudwatch_agent_repository_url" {
  description = "Dedicated retained repository for the shell-bearing CloudWatch agent image."
  value       = aws_ecr_repository.cloudwatch_agent.repository_url
}

output "cloudwatch_agent_ecr_repository" {
  description = "Name of the dedicated retained CloudWatch agent ECR repository."
  value       = aws_ecr_repository.cloudwatch_agent.name
}

output "iam_permissions_boundary_arn" {
  description = "Run-scoped permissions boundary required by Scenario A and Scenario B ECS roles."
  value       = aws_iam_policy.ecs_permissions_boundary.arn
}
