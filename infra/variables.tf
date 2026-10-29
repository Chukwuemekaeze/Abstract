variable "username" {
  description = "Non-root user created on the server (in the sudo and docker groups)."
  type        = string
  default     = "emeka"
}

variable "ssh_public_key_path" {
  description = "Public half of the key pair used to log in as that user."
  type        = string
  default     = "~/.ssh/abstract-test.pub"
}
