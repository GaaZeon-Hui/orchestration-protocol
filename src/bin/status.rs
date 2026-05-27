//! Pipeline status dashboard — read-only monitor.
//!
//! Replaces Python `status.py`.  Reads pipeline_state and audit_log,
//! renders a terminal dashboard.  Supports `--once` for agent invocation
//! and polling mode with configurable interval.

use clap::Parser;
use rusqlite::Connection;
use std::collections::HashMap;
use std::thread;
use std::time::Duration;

#[derive(Parser)]
#[command(name = "status", about = "Pipeline status dashboard")]
struct Args {
    #[arg(default_value = ".claude/orchestrator/orchestrator.db")]
    db_path: String,

    #[arg(long)]
    once: bool,

    #[arg(default_value = "3")]
    interval: u64,
}

fn main() {
    let args = Args::from_args();
    let mut last_audit_id: i64 = 0;

    loop {
        let conn = open_ro(&args.db_path);
        let (pipelines, current_audit_id) = if let Some(ref c) = conn {
            (fetch_all(c), max_audit_id(c))
        } else {
            (vec![], None)
        };

        let changed = current_audit_id.map_or(false, |id| id != last_audit_id);
        render(&pipelines, current_audit_id, changed);
        last_audit_id = current_audit_id.unwrap_or(0);

        if args.once {
            break;
        }
        if changed {
            thread::sleep(Duration::from_secs(2));
        } else {
            thread::sleep(Duration::from_secs(args.interval));
        }
    }
}

fn open_ro(db_path: &str) -> Option<Connection> {
    if !std::path::Path::new(db_path).exists() {
        return None;
    }
    let uri = format!("file:{}?mode=ro", db_path.replace('\\', "/"));
    Connection::open_with_flags(&uri, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).ok()
}

fn max_audit_id(conn: &Connection) -> Option<i64> {
    conn.query_row("SELECT COALESCE(MAX(id), 0) FROM audit_log", [], |row| {
        row.get(0)
    })
    .ok()
}

fn fetch_all(conn: &Connection) -> Vec<HashMap<String, String>> {
    let mut stmt = match conn.prepare(
        "SELECT request_id, agent, stage, revision, reason_json, approval_status, rejection_reason, updated_at, human_intervention
         FROM pipeline_state ORDER BY updated_at DESC",
    ) {
        Ok(s) => s,
        Err(_) => return vec![],
    };
    let rows = stmt
        .query_map([], |row| {
            let mut map = HashMap::new();
            for (i, col) in [
                "request_id", "agent", "stage", "revision", "reason_json",
                "approval_status", "rejection_reason", "updated_at", "human_intervention",
            ]
            .iter()
            .enumerate()
            {
                let val: Option<String> = row.get(i).ok().flatten();
                if let Some(v) = val {
                    map.insert(col.to_string(), v);
                }
            }
            Ok(map)
        })
        .ok();
    match rows {
        Some(r) => r.filter_map(|r| r.ok()).collect(),
        None => vec![],
    }
}

fn render(pipelines: &[HashMap<String, String>], audit_id: Option<i64>, changed: bool) {
    print!("\x1B[2J\x1B[H"); // clear screen

    let id = audit_id.unwrap_or(0);
    let changed_str = if changed {
        " [CHANGED — refreshed after 2s cooldown]"
    } else {
        "polling..."
    };

    println!(" Pipeline Status Dashboard");
    println!("{:=<80}", "");
    println!(
        " DB: .claude/orchestrator/orchestrator.db | audit #{:>6} | {}",
        id, changed_str
    );
    println!("{:=<80}", "");

    if pipelines.is_empty() {
        println!("\n  (no pipelines yet — waiting for worker to init)\n");
        return;
    }

    let icons: HashMap<&str, &str> = HashMap::from([
        ("init", "○"),
        ("orchestrator_gate", "◐"),
        ("worker_modify", "⚒"),
        ("reviewer_check", "◉"),
        ("orchestrator_arbiter", "◕"),
        ("verified", "✔"),
        ("rejected", "✘"),
        ("completed", "★"),
        ("lock_released", "☑"),
    ]);

    for p in pipelines {
        let stage = p.get("stage").map(|s| s.as_str()).unwrap_or("?");
        let icon = icons.get(stage).unwrap_or(&"?");
        let agent = p.get("agent").map(|a| &a[..a.len().min(6)]).unwrap_or("");
        let rev = p.get("revision").map(|r| r.as_str()).unwrap_or("?");
        let reason = p
            .get("reason_json")
            .map(|r| &r[..r.len().min(48)])
            .unwrap_or("");

        let hi_flag = if p
            .get("human_intervention")
            .map_or(false, |h| h.contains("needs_human"))
        {
            " [NEEDS HUMAN]"
        } else {
            ""
        };

        println!(
            " {:<6} | {}{:<20} | rev {:>2} | {:<48} | {}{}",
            agent,
            icon,
            stage,
            rev,
            reason,
            p.get("updated_at").map(|u| u.as_str()).unwrap_or(""),
            hi_flag,
        );

        if stage == "rejected" {
            if let Some(r) = p.get("rejection_reason") {
                println!(
                    "        | {:<22} |       | REJECTED: {}",
                    "",
                    &r[..r.len().min(40)]
                );
            }
        }
    }
    println!("{:-<80}", "");
}
