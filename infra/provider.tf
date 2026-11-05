terraform {
  required_providers {
    contabo = {
      source  = "contabo/contabo"
      version = "~> 0.1.44"
    }
    # Writes the Ansible inventory (inventory.tf); not provider-specific.
    local = {
      source  = "hashicorp/local"
      version = "~> 2.9"
    }
  }
}

# Reads the API credentials from the CNTB_OAUTH2_* environment variables.
provider "contabo" {}

# Registers the public key with Contabo so the installed OS accepts it.
resource "contabo_secret" "abstract" {
  name  = "abstract"
  type  = "ssh"
  value = trimspace(file(pathexpand(var.ssh_public_key_path)))
}

# Adopts the VPS already on the account instead of buying a new one.
# CAUTION: changing image_id, ssh_keys or default_user REINSTALLS the server.
resource "contabo_instance" "abstract" {
  existing_instance_id = "203320526"
  display_name         = "abstract-prod"
  image_id             = "d64d5c6c-9dda-4e38-8174-0ee282474d8a" # ubuntu-24.04
  default_user         = "root"
  ssh_keys             = [tonumber(contabo_secret.abstract.id)]

  # Destroying this resource cancels the paid VPS contract.
  lifecycle {
    prevent_destroy = true
  }
}

# Provider-neutral name for the server's IP; other files use local.server_ip.
locals {
  server_ip = contabo_instance.abstract.ip_config[0].v4[0].ip
}

output "server_ip" {
  value = local.server_ip
}
