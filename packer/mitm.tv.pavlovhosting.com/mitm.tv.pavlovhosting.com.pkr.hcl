variable scw_access_key {
  type = string
  default = env("SCW_ACCESS_KEY")
}

variable scw_secret_key {
  type = string
  default = env("SCW_SECRET_KEY")
}

variable scw_project_id {
  type = string
  default = env("SCW_PROJECT_ID")
}

variable scw_zone {
  type = string
  default = "nl-ams-1"
}

variable image_name {
  type = string
  default = env("IMAGE_NAME")
}

variable bucket_name {
  type = string
  default = env("BUCKET_NAME")
}

variable bucket_region {
  type = string
  default = env("BUCKET_REGION")
}

variable replay_files_url {
  type = string
  default = env("REPLAY_FILES_URL")
}

source "scaleway" "debian" {
  access_key = "${var.scw_access_key}"
  secret_key = "${var.scw_secret_key}"
  project_id = "${var.scw_project_id}"
  zone = "${var.scw_zone}"
  image = "debian_bullseye"
  commercial_type = "DEV1-S"
  ssh_username = "root"
  image_name = "${var.image_name}"
}

build {

  sources = ["source.scaleway.debian"]

  provisioner "shell" {
    inline = ["apt update && apt upgrade -y"]
  }

  provisioner "shell" {
    inline = ["apt install python3-pip -y"]
  }

  provisioner "file" {
    source = "./app"
    destination = "/app"
  }

  provisioner "shell" {
    inline = ["pip3 install -r /app/requirements.txt"]
  }

  provisioner "shell" {
    environment_vars = [
      "BUCKET_NAME=${var.bucket_name}",
      "BUCKET_REGION=${var.bucket_region}",
      "SCW_ACCESS_KEY=${var.scw_access_key}",
      "SCW_SECRET_KEY=${var.scw_secret_key}",
      "REPLAY_FILES_URL=${var.replay_files_url}",
    ]
    inline = ["envsubst < /app/environment.txt > /root/environment.txt"]
  }

  provisioner "file" {
    source = "./pavlovtv.service"
    destination = "/etc/systemd/system/pavlovtv.service"
  }

  provisioner "shell" {
    inline = ["systemctl enable pavlovtv && systemctl start pavlovtv"]
  }

}
