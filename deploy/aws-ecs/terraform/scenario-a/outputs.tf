output "resolved_inventory" {
  value     = module.scenario.resolved_inventory
  sensitive = true
}

output "acceptance_tls_ca_pem" {
  description = "Disposable self-signed ALB CA certificate; valid for only 24 hours."
  value       = module.scenario.acceptance_tls_ca_pem
  sensitive   = true
}

output "public_url" {
  value = module.scenario.public_url
}

output "cluster_name" {
  value = module.scenario.cluster_name
}

output "service_name" {
  value = module.scenario.service_name
}

output "task_role_arn" {
  value = module.scenario.task_role_arn
}

output "runtime_database_secret_arn" {
  value = module.scenario.runtime_database_secret_arn
}

output "doctor_task_definition_arn" {
  value = module.scenario.doctor_task_definition_arn
}

output "doctor_network_configuration" {
  value = module.scenario.doctor_network_configuration
}

output "service_enable_command" {
  value = module.scenario.service_enable_command
}

output "teardown" {
  value = module.scenario.teardown
}
