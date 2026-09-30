#!/usr/bin/env bash
# `make install-docker`: install Docker Engine and the Compose plugin on Ubuntu from Docker's own
# apt repository (https://docs.docker.com/engine/install/ubuntu/), plus make, git and openssl.
# Safe to re-run. Needs sudo.
set -euo pipefail

if ! grep -qi ubuntu /etc/os-release 2>/dev/null; then
  echo "This installer is for Ubuntu. See https://docs.docker.com/engine/install/ for others." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y ca-certificates curl git make openssl

if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  echo "Docker and Compose are already installed: $(docker --version)"
else
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

# Start on boot; the stack's containers restart themselves (restart: unless-stopped).
sudo systemctl enable --now docker

if ! id -nG "$USER" | grep -qw docker; then
  sudo usermod -aG docker "$USER"
  echo
  echo "Added $USER to the 'docker' group. Log out and back in (or run: newgrp docker), then: make setup"
fi
