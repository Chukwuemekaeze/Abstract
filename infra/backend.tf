# State lives in Cloudflare R2 (S3-compatible), shared by the laptop and CI.
# Credentials and the R2 address come from the environment:
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL_S3
terraform {
  backend "s3" {
    bucket = "abstract-tofu-state"
    key    = "abstract-test/terraform.tfstate"
    region = "auto"

    # Lock file in the bucket, so two applies can't run at the same time.
    use_lockfile = true

    # R2 is not AWS: skip the AWS-only checks.
    use_path_style              = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
  }
}
