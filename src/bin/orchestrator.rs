//! CLI entry point for Claude Code skill registration.
//!
//! Usage: `cargo run --bin orchestrator`
//! Outputs "ROLE: orchestrator" or "ROLE: worker" or "ROLE: reviewer"
use orchestrator::db;
use orchestrator::orchestrator::Orchestrator;
use std::fs;

fn main() {
    let db_path = ".claude/orchestrator/orchestrator.db";
    if let Some(parent) = std::path::Path::new(db_path).parent() {
        fs::create_dir_all(parent).ok();
    }

    let orch = Orchestrator::new(db_path);
    let conn = orch.connect();
    db::init_db(&conn);
    db::migrate(&conn);

    let role = orch.try_register("agent-default");
    println!("ROLE: {}", role);
}
