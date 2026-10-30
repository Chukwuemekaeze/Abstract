terraform {
  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.106"
    }
    # Writes the Ansible inventory (inventory.tf); not provider-specific.
    local = {
      source  = "hashicorp/local"
      version = "~> 2.9"
    }
  }
}

# Reads the API token from the DIGITALOCEAN_TOKEN environment variable.
provider "digitalocean" {}

# Registers the public key with DigitalOcean so new droplets accept it.
resource "digitalocean_ssh_key" "abstract_test" {
  name       = "abstract-test"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

resource "digitalocean_droplet" "abstract_test" {
  name     = "abstract-test"
  region   = "fra1"
  size     = "s-1vcpu-1gb-35gb-intel"
  image    = "ubuntu-24-04-x64"
  ssh_keys = [digitalocean_ssh_key.abstract_test.fingerprint]
}

# Provider-neutral name for the server's IP; other files use local.server_ip.
locals {
  server_ip = digitalocean_droplet.abstract_test.ipv4_address
}

output "server_ip" {
  value = local.server_ip
}
