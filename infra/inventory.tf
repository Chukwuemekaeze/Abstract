# Writes the Ansible inventory from OpenTofu's outputs, so Ansible always
# targets the current server without the IP being typed anywhere.
resource "local_file" "ansible_inventory" {
  filename        = "${path.module}/ansible/inventory.ini"
  file_permission = "0644"
  content         = <<-EOT
    [abstract_test]
    ${local.server_ip}
  EOT
}
