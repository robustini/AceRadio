#!/usr/bin/env bash
set -euo pipefail

repo_archive_url="https://github.com/robustini/AceRadio/archive/refs/heads/main.zip"
working_root="$(pwd)"
acestep_dir="$working_root/acestep"

if [ ! -d "$acestep_dir" ]; then
  echo "Current directory does not look like an ACE-Step root. Missing folder: $acestep_dir" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required but was not found in PATH." >&2
  exit 1
fi

if ! command -v unzip >/dev/null 2>&1; then
  echo "unzip is required but was not found in PATH." >&2
  exit 1
fi

tmp_root="$(mktemp -d)"
zip_path="$tmp_root/aceradio_main.zip"
extract_dir="$tmp_root/extract"

cleanup() {
  rm -rf "$tmp_root"
}
trap cleanup EXIT

mkdir -p "$extract_dir"

echo "Downloading AceRadio repository archive..."
curl -fL "$repo_archive_url" -o "$zip_path"

echo "Extracting archive..."
unzip -q "$zip_path" -d "$extract_dir"

repo_root="$extract_dir/AceRadio-main"
if [ ! -d "$repo_root" ]; then
  repo_root="$(find "$extract_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
fi

if [ -z "${repo_root:-}" ] || [ ! -d "$repo_root" ]; then
  echo "Unable to locate extracted repository root." >&2
  exit 1
fi

source_ui="$repo_root/acestep/ui/aceradio"
source_bat="$repo_root/start_aceradio_ui.bat"
source_sh="$repo_root/start_aceradio_ui.sh"

if [ ! -d "$source_ui" ]; then
  echo "Missing source folder in archive: acestep/ui/aceradio" >&2
  exit 1
fi
if [ ! -f "$source_bat" ]; then
  echo "Missing source file in archive: start_aceradio_ui.bat" >&2
  exit 1
fi
if [ ! -f "$source_sh" ]; then
  echo "Missing source file in archive: start_aceradio_ui.sh" >&2
  exit 1
fi

target_ui_parent="$working_root/acestep/ui"
target_ui="$target_ui_parent/aceradio"
target_bat="$working_root/start_aceradio_ui.bat"
target_sh="$working_root/start_aceradio_ui.sh"

mkdir -p "$target_ui_parent"

if [ -d "$target_ui" ]; then
  echo "Removing previous acestep/ui/aceradio..."
  rm -rf "$target_ui"
fi

echo "Installing AceRadio files..."
cp -R "$source_ui" "$target_ui_parent/"
cp "$source_bat" "$target_bat"
cp "$source_sh" "$target_sh"
chmod +x "$target_sh"

echo
echo "AceRadio installation completed."
echo "To run AceRadio, launch start_aceradio_ui.bat or start_aceradio_ui.sh from the ACE-Step root."
