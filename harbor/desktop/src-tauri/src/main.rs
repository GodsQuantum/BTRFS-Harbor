#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    if let Err(error) = btrfs_harbor_desktop_lib::reexec_appimage_extract_and_run_if_needed() {
        eprintln!("{error}");
        std::process::exit(1);
    }
    btrfs_harbor_desktop_lib::run();
}
