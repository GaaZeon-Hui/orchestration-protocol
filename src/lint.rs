//! Lint layer — boundary checks and plan validation.
//!
//! Replaces Python `lint.py`.  AST hints (`_extract_hints`) are **dropped**
//! because no mature Python AST parser exists in the Rust ecosystem, and the
//! feature had no consumer in the current 3-role architecture.
//!
//! Retained: `run_lint`, `lint_plan` (for orchestrator_gate), `lint_changed_files`
//! (for reviewer_check cross-validation), `validate_plan` (code-assertion plan schema check).

use serde_json::Value as JsonValue;
use std::collections::HashMap;
use std::process::Command;

// ── Pattern matching ───────────────────────────────────────

fn match_pattern(path: &str, pattern: &str) -> bool {
    let pattern = pattern.replace('\\', "/");
    if pattern.ends_with('/') {
        return path.starts_with(&pattern);
    }
    if pattern.starts_with("*.") {
        return path.ends_with(&pattern[1..]);
    }
    if pattern.ends_with("/**") {
        return path.starts_with(&pattern[..pattern.len() - 3]);
    }
    path == pattern || path.starts_with(&pattern)
}

// ── Boundary check ─────────────────────────────────────────

#[derive(Debug, PartialEq)]
pub struct LintResult {
    pub blocked: bool,
    pub reason: String,
}

pub fn run_lint(
    files: &[String],
    boundaries: &HashMap<String, JsonValue>,
    agent: &str,
) -> LintResult {
    if boundaries.is_empty() {
        return LintResult {
            blocked: true,
            reason: "boundaries not configured".into(),
        };
    }
    let Some(agent_rules) = boundaries.get(agent) else {
        return LintResult {
            blocked: true,
            reason: format!("agent '{}' not found in boundaries", agent),
        };
    };

    let forbidden: Vec<&str> = agent_rules["forbidden"]
        .as_array()
        .map(|a| a.iter().filter_map(|v| v.as_str()).collect())
        .unwrap_or_default();
    let can_touch: Vec<&str> = agent_rules["can_touch"]
        .as_array()
        .map(|a| a.iter().filter_map(|v| v.as_str()).collect())
        .unwrap_or_default();

    for f in files {
        let f = f.replace('\\', "/");
        for pat in &forbidden {
            if match_pattern(&f, pat) {
                return LintResult {
                    blocked: true,
                    reason: format!("{} matches forbidden pattern {}", f, pat),
                };
            }
        }
        if !can_touch.is_empty() && !can_touch.iter().any(|p| match_pattern(&f, p)) {
            return LintResult {
                blocked: true,
                reason: format!("{} not in can_touch", f),
            };
        }
    }

    LintResult {
        blocked: false,
        reason: String::new(),
    }
}

// ── Plan validation ────────────────────────────────────────

/// Validate plan_json structure.  Returns parsed JSON on success, error string on failure.
pub fn validate_plan(plan_json: &str) -> Result<JsonValue, String> {
    let p: JsonValue =
        serde_json::from_str(plan_json).map_err(|e| format!("plan_json is not valid JSON: {}", e))?;
    let files = p
        .get("files")
        .and_then(|f| f.as_array())
        .ok_or("files is missing or not an array")?;
    if files.is_empty() {
        return Err("files is empty".into());
    }
    for f in files {
        let s = f.as_str().ok_or("files contains non-string entry")?;
        if s.starts_with('/') || s.contains("..") {
            return Err(format!("path traversal in files: {}", s));
        }
    }
    Ok(p)
}

/// Entry point for `orchestrator_gate` stage: validate plan structure, then run boundary check.
pub fn lint_plan(
    plan_json: &str,
    boundaries: &HashMap<String, JsonValue>,
    agent: &str,
) -> LintResult {
    match validate_plan(plan_json) {
        Err(reason) => LintResult {
            blocked: true,
            reason,
        },
        Ok(plan) => {
            let files: Vec<String> = plan["files"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(String::from))
                        .collect()
                })
                .unwrap_or_default();
            run_lint(&files, boundaries, agent)
        }
    }
}

/// Entry point for `reviewer_check` stage: read actual git diff, feed to boundary check.
pub fn lint_changed_files(
    boundaries: &HashMap<String, JsonValue>,
    agent: &str,
) -> LintResult {
    let output = Command::new("git").args(["diff", "--name-only"]).output();
    match output {
        Ok(o) if o.status.success() => {
            let raw = String::from_utf8_lossy(&o.stdout);
            if raw.trim().is_empty() {
                return LintResult {
                    blocked: false,
                    reason: String::new(),
                };
            }
            let files: Vec<String> = raw.trim().lines().map(String::from).collect();
            run_lint(&files, boundaries, agent)
        }
        _ => LintResult {
            blocked: true,
            reason: "git diff failed".into(),
        },
    }
}

// ── Tests ──────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_boundaries() -> HashMap<String, JsonValue> {
        let json = serde_json::json!({
            "py-agent": {
                "can_touch": ["*.py", "lib/"],
                "forbidden": ["app/", "core/__init__.py"]
            }
        });
        json.as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect()
    }

    #[test]
    fn test_validate_plan_valid() {
        assert!(validate_plan(r#"{"files":["a.py"]}"#).is_ok());
    }

    #[test]
    fn test_validate_plan_empty_files() {
        assert!(validate_plan(r#"{"files":[]}"#).is_err());
    }

    #[test]
    fn test_validate_plan_not_json() {
        assert!(validate_plan("not json").is_err());
    }

    #[test]
    fn test_validate_plan_path_traversal() {
        assert!(validate_plan(r#"{"files":["../etc/passwd"]}"#).is_err());
    }

    #[test]
    fn test_validate_plan_non_string_entry() {
        assert!(validate_plan(r#"{"files":[123]}"#).is_err());
    }

    #[test]
    fn test_lint_plan_boundary_blocked() {
        let boundaries = sample_boundaries();
        let result = lint_plan(r#"{"files":["app/main.py"]}"#, &boundaries, "py-agent");
        assert!(result.blocked);
    }

    #[test]
    fn test_lint_plan_passes() {
        let boundaries = sample_boundaries();
        let result = lint_plan(r#"{"files":["lib/utils.py"]}"#, &boundaries, "py-agent");
        assert!(!result.blocked);
    }

    #[test]
    fn test_match_pattern_directory() {
        assert!(match_pattern("app/main.py", "app/"));
        assert!(!match_pattern("lib/util.py", "app/"));
    }

    #[test]
    fn test_match_pattern_glob() {
        assert!(match_pattern("main.py", "*.py"));
        assert!(!match_pattern("main.md", "*.py"));
    }
}
