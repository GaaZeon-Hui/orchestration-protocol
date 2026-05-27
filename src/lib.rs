//! Orchestration Protocol — Rust edition.
//!
//! Multi-agent pipeline orchestration with SQLite-backed state machine.
//! 3 roles (Worker / Orchestrator / Reviewer), 7 named stages,
//! round-based correction loop, CAS optimistic locking via revision counter.

pub mod db;
pub mod lint;
pub mod orchestrator;
pub mod pipeline;
