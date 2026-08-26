//! The JSON envelope every command answers in.
//!
//! One shape for every command, so an agent parses the outcome before it parses
//! the answer: the eight keys are always present, `status` says which of `data`
//! and `errors` carries the meaning, and a failure is a well-formed envelope on
//! **stdout** with a non-zero exit — never a stack trace on stderr that has to
//! be told apart from a diagnostic.
//!
//! `details` on an error is the self-correction path: a name that did not
//! resolve comes back with what did, so the next call is a correction rather
//! than a search.

use serde::Serialize;
use serde_json::{Value, json};

pub const TOOL: &str = "rtlscanner";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// The closed set of failures a command can report. A caller branches on the
/// code; the message is for a human and may be rephrased.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum ErrorCode {
    /// A path given on the command line does not exist.
    InputNotFound,
    /// The file is not a design database, or is a schema version this build
    /// does not read.
    DbUnreadable,
    /// The database is readable but a required part of it is not.
    BadDb,
    /// A path did not name a signal in this design.
    SignalNotFound,
    /// The design has no top to resolve against, or several and none named.
    NoTop,
    /// A bit-select the signal cannot be measured against.
    BadSelect,
}

impl ErrorCode {
    pub fn tag(self) -> &'static str {
        match self {
            ErrorCode::InputNotFound => "INPUT_NOT_FOUND",
            ErrorCode::DbUnreadable => "DB_UNREADABLE",
            ErrorCode::BadDb => "BAD_DB",
            ErrorCode::SignalNotFound => "SIGNAL_NOT_FOUND",
            ErrorCode::NoTop => "NO_TOP",
            ErrorCode::BadSelect => "BAD_SELECT",
        }
    }
}

/// A failure with the structured context a caller needs to correct itself.
#[derive(Debug, Clone)]
pub struct CommandError {
    pub code: ErrorCode,
    pub message: String,
    pub details: Option<Value>,
}

impl CommandError {
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        CommandError { code, message: message.into(), details: None }
    }

    pub fn with_details(mut self, details: Value) -> Self {
        self.details = Some(details);
        self
    }
}

/// A note about the answer that is not a failure: a source file that has moved
/// on from the export, a count the caller should look at.
#[derive(Debug, Clone, Serialize)]
pub struct Diagnostic {
    pub severity: &'static str,
    pub message: String,
}

impl Diagnostic {
    pub fn warning(message: impl Into<String>) -> Self {
        Diagnostic { severity: "warning", message: message.into() }
    }
}

/// What a command produces: the typed answer, or the failure that replaced it.
///
/// Both views render from one set of typed fields, so they cannot drift apart —
/// the class of bug that shows up as a field one view has and the other does
/// not.
pub trait CommandResult {
    /// The answer and its summary.
    fn to_json(&self) -> (Value, Value);
    /// The same answer for a terminal.
    fn render_human(&self) -> String;
}

/// What to write and what to exit with. Rendering is separated from printing so
/// the envelope can be asserted as a value.
pub struct Rendered {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
}

/// Render an outcome. JSON puts everything on stdout, including a failure;
/// the human view keeps notes and failures on stderr, where a terminal
/// expects them.
pub fn render<R: CommandResult>(
    command: &str,
    args: Value,
    outcome: &Result<R, CommandError>,
    diagnostics: &[Diagnostic],
    json: bool,
) -> Rendered {
    match outcome {
        Ok(result) => {
            let (data, summary) = result.to_json();
            if json {
                Rendered {
                    stdout: envelope(command, args, "ok", data, summary, diagnostics, json!([])),
                    stderr: String::new(),
                    exit_code: 0,
                }
            } else {
                Rendered {
                    stdout: result.render_human(),
                    stderr: diagnostics
                        .iter()
                        .map(|d| format!("{}: {}\n", d.severity, d.message))
                        .collect(),
                    exit_code: 0,
                }
            }
        }
        Err(e) => {
            let errors = json!([{
                "code": e.code.tag(),
                "message": e.message,
                "details": e.details,
            }]);
            if json {
                Rendered {
                    stdout: envelope(
                        command,
                        args,
                        "error",
                        Value::Null,
                        Value::Null,
                        diagnostics,
                        errors,
                    ),
                    stderr: String::new(),
                    exit_code: 1,
                }
            } else {
                Rendered {
                    stdout: String::new(),
                    stderr: format!("error: {}\n", e.message),
                    exit_code: 1,
                }
            }
        }
    }
}

fn envelope(
    command: &str,
    args: Value,
    status: &str,
    data: Value,
    summary: Value,
    diagnostics: &[Diagnostic],
    errors: Value,
) -> String {
    let env = json!({
        "tool": TOOL,
        "version": VERSION,
        "status": status,
        "command": { "name": command, "args": args },
        "data": data,
        "diagnostics": diagnostics,
        "errors": errors,
        "summary": summary,
    });
    let mut out = serde_json::to_string_pretty(&env).expect("envelope is JSON by construction");
    out.push('\n');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Answer;
    impl CommandResult for Answer {
        fn to_json(&self) -> (Value, Value) {
            (json!({ "x": 1 }), json!({ "results": 1 }))
        }
        fn render_human(&self) -> String {
            "x = 1\n".into()
        }
    }

    /// The eight keys, sorted: a JSON object is unordered, so presence is the
    /// contract and order is not.
    const KEYS: [&str; 8] =
        ["command", "data", "diagnostics", "errors", "status", "summary", "tool", "version"];

    fn json_of(r: &Rendered) -> Value {
        serde_json::from_str(&r.stdout).unwrap()
    }

    #[test]
    fn a_successful_answer_carries_the_eight_keys_and_exits_zero() {
        let ok: Result<Answer, CommandError> = Ok(Answer);
        let r = render("info", json!({ "db": "/p/design.db" }), &ok, &[], true);
        let v = json_of(&r);

        assert_eq!(v.as_object().unwrap().keys().collect::<Vec<_>>(), KEYS);
        assert_eq!(v["status"], "ok");
        assert_eq!(v["tool"], TOOL);
        assert_eq!(v["data"]["x"], 1);
        assert_eq!(v["summary"]["results"], 1);
        assert_eq!(v["errors"].as_array().unwrap().len(), 0);
        // The envelope says what it was asked, so a caller need not recall it.
        assert_eq!(v["command"]["name"], "info");
        assert_eq!(v["command"]["args"]["db"], "/p/design.db");
        assert_eq!(r.exit_code, 0);
    }

    #[test]
    fn a_failure_is_an_envelope_on_stdout_with_a_non_zero_exit() {
        let e = CommandError::new(ErrorCode::DbUnreadable, "boom")
            .with_details(json!({ "path": "/p/x.db" }));
        let failed: Result<Answer, CommandError> = Err(e);
        let r = render("info", json!({}), &failed, &[], true);
        let v = json_of(&r);

        assert_eq!(v.as_object().unwrap().keys().collect::<Vec<_>>(), KEYS);
        assert_eq!(v["status"], "error");
        // A caller reads the outcome before the answer, so neither is a stale
        // half-answer left over from the attempt.
        assert!(v["data"].is_null());
        assert!(v["summary"].is_null());
        assert_eq!(v["errors"][0]["code"], "DB_UNREADABLE");
        assert_eq!(v["errors"][0]["details"]["path"], "/p/x.db");
        assert!(r.stderr.is_empty(), "JSON keeps the failure on stdout");
        assert_eq!(r.exit_code, 1);
    }

    #[test]
    fn a_diagnostic_travels_with_a_successful_answer() {
        let ok: Result<Answer, CommandError> = Ok(Answer);
        let notes = [Diagnostic::warning("dut.sv has changed since the export")];

        let v = json_of(&render("info", json!({}), &ok, &notes, true));
        assert_eq!(v["status"], "ok");
        assert_eq!(v["diagnostics"][0]["severity"], "warning");
        assert!(v["diagnostics"][0]["message"].as_str().unwrap().contains("changed"));

        let human = render("info", json!({}), &ok, &notes, false);
        assert_eq!(human.stdout, "x = 1\n");
        assert!(human.stderr.contains("warning: dut.sv has changed"));
        assert_eq!(human.exit_code, 0);
    }

    #[test]
    fn the_human_view_puts_a_failure_on_stderr_and_prints_no_answer() {
        let failed: Result<Answer, CommandError> =
            Err(CommandError::new(ErrorCode::InputNotFound, "no such file"));
        let r = render("info", json!({}), &failed, &[], false);
        assert!(r.stdout.is_empty());
        assert_eq!(r.stderr, "error: no such file\n");
        assert_eq!(r.exit_code, 1);
    }
}
