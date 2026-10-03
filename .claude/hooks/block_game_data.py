#!/usr/bin/env python3
"""Claude Code PreToolUse hook: never write into a Skyrim game Data folder (CLAUDE.md rule 3).

Protocol (Claude Code hooks): the tool call arrives as JSON on stdin,
  {"tool_name": "Write", "tool_input": {"file_path": ...}, "cwd": ...}
Exit code 0 lets the call run; exit code 2 blocks it and the stderr text is shown to Claude.
settings.json runs this file through block_game_data.sh, which also blocks when no Python 3 exists
(any exit code other than 2 would let the call through).

Blocked:
  Write / Edit / MultiEdit / NotebookEdit  whose target path (as given, or after resolving
      symlinks) lies in ".../Skyrim Special Edition/Data" (either slash, any letter case).
  Bash  commands that could write into, or delete from, such a folder:
      - copy/move/link/sync whose destination is Data (or the game folder that contains it);
      - mv / rsync --remove-source-files whose source is in Data (that deletes game files);
      - output redirection into Data;
      - any other command that is not read-only and names Data or the game folder
        (tar, unzip, rm, mkdir, sed -i, xxd -r, python -c, bash -c, ...);
      - any non-read-only command run after `cd` into Data or the game folder (also when
        the hook's cwd is already there), including a relative "Data/..." from the game folder;
      - the same rules applied to the commands inside $(...), `...`, <(...) and >(...);
      - a here-document or text naming Data fed into a non-read-only command, or piped into a
        shell, interpreter or xargs.
  Plain reads (ls, cat, find, grep, ...) stay allowed, and so does text that only *mentions* the
  folder in a git/gh message or a pytest -k/-m expression.
Limits: a path hidden in a variable or built at run time cannot be seen; build into
dist/CardboardFaceMask/ and let the user install it.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

# A game Data folder. In a shell word the name may end at whitespace, a quote or an operator
# (words can hold whole scripts, e.g. `bash -c '...'`); in a tool's file path only at a separator
# or the end ("Data backup.txt" next to Data is not inside it).
DATA_RE = re.compile(r"skyrim special edition[\\/]+data(?=$|[\\/\s\"'`;|&()<>])", re.IGNORECASE)
DATA_PATH_RE = re.compile(r"skyrim special edition[\\/]+data(?=$|[\\/])", re.IGNORECASE)
# The game folder itself (the folder that contains Data), not a path below it.
GAME_ROOT_RE = re.compile(r"skyrim special edition[\\/]*(?=$|[\s\"'`;|&()<>])", re.IGNORECASE)
# "Data" or "./Data/..." relative to the game folder.
REL_DATA_RE = re.compile(r"^(?:\.[\\/]+)*data(?=$|[\\/])", re.IGNORECASE)
# Anything that names the game folder at all: commands without it are not analysed further.
GAME_RE = re.compile(r"skyrim special edition", re.IGNORECASE)

READ_ONLY = {
    "ls", "cat", "head", "tail", "more", "stat", "du", "df", "wc", "grep", "egrep", "fgrep", "rg",
    "diff", "cmp", "md5sum", "sha1sum", "sha256sum", "shasum", "md5", "realpath", "readlink",
    "basename", "dirname", "hexdump", "od", "strings", "test", "[", "echo", "printf", "cd",
    "pushd", "popd", "true", "false",
}  # deliberately not here: xxd (-r writes), tree (-o writes), less (-o writes), file (-C writes)
FIND_WRITES = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
COPY_LIKE = {"cp", "mv", "rsync", "install", "ln", "ditto", "scp", "gcp", "gmv"}
MOVE_LIKE = {"mv", "gmv"}
REMOVE_SOURCE_OPTIONS = {"--remove-source-files", "--remove-sent-files"}
WRAPPERS = {"sudo", "env", "command", "nice", "nohup", "time", "exec", "builtin", "xargs", "doas"}
# Commands that run text they receive on stdin (a pipe from `echo "cp x Data/"` would write).
STDIN_RUNNERS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "perl", "ruby", "node", "xargs",
                 "parallel", "eval", "source", "."}
# Options whose value is free text, per command (commit messages, test selections).
TEXT_OPTIONS = {"git": {"-m", "--message"}, "gh": {"-b", "--body", "-t", "--title"},
                "pytest": {"-k", "-m"}}
SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", ";;", "\n"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<>"}
HARMLESS_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr"}
HEREDOC_MARK = "__HEREDOC_{}__"
MAX_DEPTH = 8


class ParseError(ValueError):
    """The command could not be split reliably (unbalanced quote or substitution)."""


def is_data_path(text: str) -> bool:
    """A shell word or command text that mentions a game Data folder."""
    return bool(DATA_RE.search(text.replace("\\ ", " ")))


def is_data_file_path(path: str) -> bool:
    """A file path that lies inside (or is) a game Data folder."""
    return bool(DATA_PATH_RE.search(path))


def mentions_game(text: str) -> bool:
    """Any mention of the game folder name (cheap pre-filter)."""
    return bool(GAME_RE.search(text.replace("\\ ", " ")))


def names_data_or_game(text: str) -> bool:
    """Text naming a Data folder or the game folder itself (not e.g. My Games\\...\\SKSE)."""
    text = text.replace("\\ ", " ")
    return bool(DATA_RE.search(text) or GAME_ROOT_RE.search(text))


# --------------------------------------------------------------------------- file tools

def file_target_reason(tool_input: dict, cwd: str) -> str | None:
    for key in ("file_path", "notebook_path", "path"):
        raw = tool_input.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        candidates = [raw]
        try:
            candidates.append(os.path.realpath(os.path.join(cwd, os.path.expanduser(raw))))
        except (OSError, ValueError):
            pass
        for candidate in candidates:
            if is_data_file_path(candidate):
                return f"{key} {raw!r} is inside a Skyrim Data folder"
    return None


# --------------------------------------------------------------------------- bash: scanning

def skip_backticks(text: str, i: int) -> int:
    """Index of the backtick closing the one before text[i]."""
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "`":
            return i
        i += 1
    raise ParseError("unclosed backtick")


def read_delimiter(text: str, i: int) -> tuple[str, bool, int]:
    """Here-document delimiter word starting at text[i] -> (word, quoted?, end index)."""
    while i < len(text) and text[i] in " \t":
        i += 1
    word, quoted, quote = [], False, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            else:
                word.append(ch)
        elif ch in "'\"":
            quote, quoted = ch, True
        elif ch == "\\" and i + 1 < len(text):
            quoted = True
            word.append(text[i + 1])
            i += 1
        elif ch.isspace() or ch in ";|&<>()":
            break
        else:
            word.append(ch)
        i += 1
    if not word:
        raise ParseError("here-document without a delimiter")
    return "".join(word), quoted, i


def skip_heredoc_bodies(text: str, i: int, pending: list) -> tuple[int, list[str]]:
    """text[i] starts the line after a here-document operator: consume one body per pending
    (delimiter, strip_tabs) entry. Returns the index after the last delimiter line and the bodies."""
    bodies = []
    for delim, strip_tabs in pending:
        lines = []
        while i < len(text):
            nl = text.find("\n", i)
            end = len(text) if nl < 0 else nl
            line, i = text[i:end], end + 1
            if (line.lstrip("\t") if strip_tabs else line) == delim:
                break
            lines.append(line)
        bodies.append("\n".join(lines))
    return min(i, len(text)), bodies


def match_paren(text: str, i: int) -> int:
    """text[i] is just after '$(' / '<(' / '>(': index of the matching ')'.
    Quotes, nested substitutions, backticks and here-document bodies are skipped."""
    depth, pending = 1, []
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise ParseError("unclosed quote inside $(...)")
            i = end + 1
            continue
        if ch == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                elif text.startswith("$(", i):
                    i = match_paren(text, i + 2)
                elif text[i] == "`":
                    i = skip_backticks(text, i + 1)
                i += 1
            if i >= len(text):
                raise ParseError("unclosed quote inside $(...)")
            i += 1
            continue
        if ch == "`":
            i = skip_backticks(text, i + 1) + 1
            continue
        if text.startswith("<<<", i):
            i += 3
            continue
        if text.startswith("<<", i):
            j = i + 2
            strip = j < len(text) and text[j] == "-"
            delim, _, i = read_delimiter(text, j + strip)
            pending.append((delim, strip))
            continue
        if ch == "\n" and pending:
            i, _ = skip_heredoc_bodies(text, i + 1, pending)
            pending = []
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ParseError("unclosed $(...)")


def substitution_word(body: str) -> str:
    """Stands in for $(...) in the outer command: a Data path if the body names Data or the game
    folder (its output may well be such a path), else a neutral word."""
    return "Skyrim Special Edition/Data/_subst_" if names_data_or_game(body) else "_subst_"


def preprocess(command: str) -> tuple[str, list[tuple[str, bool]], list[str]]:
    """Cut here-document bodies out of the command (each operator becomes '<< __HEREDOC_n__')
    and replace every $(...), `...`, <(...) and >(...) by a stand-in word.
    Returns (outer text, here-document (body, quoted delimiter) list, substitution bodies)."""
    out, heredocs, subs, pending = [], [], [], []
    quote, i = None, 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote != "'":
            out.append(command[i:i + 2])
            i += 2
            continue
        if quote == "'":
            quote = None if ch == "'" else quote
            out.append(ch)
            i += 1
            continue
        starts_subst = command.startswith("$(", i) or (quote is None and command[i:i + 2] in ("<(", ">("))
        if ch == "`" or starts_subst:
            end = skip_backticks(command, i + 1) if ch == "`" else match_paren(command, i + 2)
            body = command[i + 1:end] if ch == "`" else command[i + 2:end]
            subs.append(body)
            word = substitution_word(body)
            out.append(word if quote else f'"{word}"')   # keep it one shell word
            i = end + 1
            continue
        if quote == '"':
            quote = None if ch == '"' else quote
            out.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
        elif command.startswith("<<<", i):   # here-string: a plain word follows
            out.append("<<<")
            i += 3
            continue
        elif command.startswith("<<", i):
            j = i + 2
            strip = j < len(command) and command[j] == "-"
            delim, quoted, i = read_delimiter(command, j + strip)
            pending.append((delim, strip, quoted))
            out.append(" << " + HEREDOC_MARK.format(len(heredocs) + len(pending) - 1) + " ")
            continue
        elif ch == "\n" and pending:
            i, bodies = skip_heredoc_bodies(command, i + 1, [(d, s) for d, s, _ in pending])
            heredocs += [(body, quoted) for body, (_, _, quoted) in zip(bodies, pending)]
            pending = []
            out.append("\n")
            continue
        out.append(ch)
        i += 1
    if quote:
        raise ParseError("unclosed quote")
    heredocs += [("", quoted) for _, _, quoted in pending]   # operator on the last line: empty body
    return "".join(out), heredocs, subs


def mark_line_breaks(command: str) -> str:
    """Turn unquoted newlines into ';' so every line of a script is its own command
    (shlex treats a newline as plain whitespace)."""
    out, quote, escaped = [], None, False
    for ch in command:
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            ch = " ; "
        out.append(ch)
    return "".join(out)


def tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(mark_line_breaks(command), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def split_segments(tokens: list[str]) -> list[tuple[list[str], str]]:
    """Simple commands with the separator that follows each ('' at the end)."""
    segments, current = [], []
    for tok in tokens:
        if tok in SEPARATORS:
            if current:
                segments.append((current, tok))
            current = []
        else:
            current.append(tok)
    if current:
        segments.append((current, ""))
    return segments


# --------------------------------------------------------------------------- bash: one command

def strip_wrappers(seg: list[str]) -> list[str]:
    """Drop VAR=value prefixes and wrappers like sudo/env/xargs (with their options)."""
    i = 0
    while i < len(seg):
        tok = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
        elif tok in WRAPPERS:
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 1
        else:
            break
    return seg[i:]


def redirect_targets(seg: list[str]) -> list[str]:
    return [seg[i + 1] for i, tok in enumerate(seg[:-1]) if tok in REDIRECTS]


def fed_text(seg: list[str], heredocs: list[tuple[str, bool]]) -> list[str]:
    """Text a command gets on stdin: here-document bodies and <<< here-strings."""
    texts = []
    for i, tok in enumerate(seg[:-1]):
        mark = re.fullmatch(r"__HEREDOC_(\d+)__", seg[i + 1])
        if tok == "<<" and mark and int(mark.group(1)) < len(heredocs):
            texts.append(heredocs[int(mark.group(1))][0])
        elif tok == "<<<":
            texts.append(seg[i + 1])
    return texts


def without_redirects(seg: list[str]) -> list[str]:
    out, skip = [], False
    for tok in seg:
        if skip:
            skip = False
            continue
        if tok in REDIRECTS or tok in {"<", "<<", "<<<"}:
            skip = True
            # "2>" is tokenized as "2", ">": drop a lone fd number too
            if out and out[-1].isdigit():
                out.pop()
            continue
        out.append(tok)
    return out


def is_relative(path: str) -> bool:
    return not path.startswith(("/", "~", "$")) and not re.match(r"^[A-Za-z]:[\\/]", path)


def place(word: str, state: str | None) -> str | None:
    """Where a shell word points: 'data' (a Data folder or inside it), 'game' (the game folder
    that contains Data) or None. state is where the command runs (after cd)."""
    if is_data_path(word):
        return "data"
    if is_relative(word):
        if state == "data":
            return "data"
        if state == "game" and REL_DATA_RE.match(word):
            return "data"
        if state == "game" and word in (".", "./"):
            return "game"
    if GAME_ROOT_RE.search(word.replace("\\ ", " ")):
        return "game"
    return None


def after_cd(target: str | None, state: str | None) -> str | None:
    """Location state after `cd target`."""
    if target is None or target in ("~", "-"):
        return None
    where = place(target, state)
    if where:
        return where
    if state == "data" and re.fullmatch(r"\.\.[\\/]?", target):
        return "game"
    return None


def text_values(cmd: str, args: list[str]) -> set[int]:
    """Indices of args that are free text (git -m "msg", pytest -k "expr")."""
    options = TEXT_OPTIONS.get(cmd, set())
    found = set()
    for i, arg in enumerate(args):
        if arg in options and i + 1 < len(args):
            found.add(i + 1)
        elif any(arg.startswith(o + "=") or (len(o) == 2 and arg.startswith(o) and len(arg) > 2)
                 for o in options):
            found.add(i)
    return found


def command_name(words: list[str]) -> tuple[str, list[str]]:
    cmd, args = os.path.basename(words[0]), words[1:]
    if cmd.startswith("python") and args[:2] == ["-m", "pytest"]:
        return "pytest", args[2:]
    return cmd, args


def copy_operands(args: list[str]) -> tuple[list[str], str | None]:
    """(sources, destination) of a cp/mv/rsync-like command line."""
    for i, tok in enumerate(args):
        if tok in ("-t", "--target-directory") and i + 1 < len(args):
            rest = args[:i] + args[i + 2:]
            return [a for a in rest if not a.startswith("-")], args[i + 1]
        if tok.startswith("--target-directory="):
            return [a for a in args if not a.startswith("-")], tok.split("=", 1)[1]
    operands = [a for a in args if not a.startswith("-")]
    if len(operands) < 2:
        return operands, None
    return operands[:-1], operands[-1]


def copy_reason(cmd: str, args: list[str], state: str | None, mentions: bool) -> str | None:
    sources, dest = copy_operands(args)
    if dest is None:
        return f"'{cmd}' with an unclear destination near a Skyrim Data folder" if mentions or state else None
    if place(dest, state):
        return f"'{cmd}' into a Skyrim Data folder ({dest!r})"
    removes_sources = cmd in MOVE_LIKE or (cmd == "rsync" and REMOVE_SOURCE_OPTIONS.intersection(args))
    if removes_sources and any(place(src, state) for src in sources):
        return f"'{cmd}' would remove files from a Skyrim Data folder"
    return None


def segment_reason(seg: list[str], state: str | None, heredocs: list[tuple[str, bool]]) -> str | None:
    for target in redirect_targets(seg):
        if target not in HARMLESS_TARGETS and place(target, state):
            return f"output redirection into a Skyrim Data folder ({target!r})"
    words = strip_wrappers(without_redirects(seg))
    if not words:
        return None
    cmd, args = command_name(words)
    text = text_values(cmd, args)
    named = [a for i, a in enumerate(args) if i not in text]
    mentions = any(place(w, state) for w in [words[0], *named])
    if cmd not in READ_ONLY and any(names_data_or_game(t) for t in fed_text(seg, heredocs)):
        return f"a here-document or here-string naming a Skyrim game folder is fed to '{cmd}'"
    if cmd in READ_ONLY:
        return None
    if cmd == "find" and not FIND_WRITES.intersection(args):
        return None
    if cmd in COPY_LIKE:
        return copy_reason(cmd, args, state, mentions)
    if mentions:
        return f"'{cmd}' may modify a Skyrim Data folder"
    if state:
        return f"'{cmd}' runs inside the Skyrim game folder (after cd)"
    return None


def feeds_runner(seg: list[str], sep: str, next_seg: list[str] | None,
                 heredocs: list[tuple[str, bool]]) -> str | None:
    """`echo "cp x Data/" | bash`, `cat <<EOF | sh`, `find Data | xargs rm`: text naming the game
    folder piped into something that executes it."""
    if sep not in ("|", "|&") or not next_seg:
        return None
    first = next((w for w in next_seg if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)), "")
    unwrapped = strip_wrappers(next_seg)
    names = {os.path.basename(first), os.path.basename(unwrapped[0]) if unwrapped else ""}
    runners = [n for n in names if n in STDIN_RUNNERS or n.startswith("python")]
    if runners and any(names_data_or_game(t) for t in [*seg, *fed_text(seg, heredocs)]):
        return f"text naming a Skyrim game folder is piped into '{runners[0]}'"
    return None


# --------------------------------------------------------------------------- bash: whole command

def analyse(command: str, state: str | None, depth: int = 0) -> str | None:
    if depth > MAX_DEPTH:
        raise ParseError("substitutions nested too deeply")
    outer, heredocs, subs = preprocess(command)
    for body, quoted in heredocs:
        if not quoted:   # an unquoted here-document still runs $(...) inside its body
            for sub in preprocess(body)[2]:
                reason = analyse(sub, state, depth + 1)
                if reason:
                    return reason
    for sub in subs:
        reason = analyse(sub, state, depth + 1)
        if reason:
            return reason
    segments = split_segments(tokenize(outer))
    stack: list[str | None] = []
    for n, (seg, sep) in enumerate(segments):
        reason = segment_reason(seg, state, heredocs)
        nxt = segments[n + 1][0] if n + 1 < len(segments) else None
        reason = reason or feeds_runner(seg, sep, nxt, heredocs)
        if reason:
            return reason
        words = strip_wrappers(seg)
        if words and words[0] in ("cd", "pushd"):
            if words[0] == "pushd":
                stack.append(state)
            state = after_cd(words[1] if len(words) > 1 else None, state)
        elif words and words[0] == "popd":
            state = stack.pop() if stack else None
    return None


def bash_reason(command: str, cwd: str | None = None) -> str | None:
    state = after_cd(cwd, None) if cwd else None
    if state is None and not mentions_game(command):
        return None
    try:
        return analyse(command, state)
    except (ParseError, ValueError) as e:
        return f"cannot parse a command that mentions a Skyrim game folder ({e})"


# --------------------------------------------------------------------------- main

def decide(payload: dict) -> str | None:
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd")
    if tool == "Bash":
        return bash_reason(str(tool_input.get("command", "")), cwd if isinstance(cwd, str) else None)
    return file_target_reason(tool_input, str(cwd or os.getcwd()))


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
        reason = decide(payload) if isinstance(payload, dict) else None
    except ValueError:
        # Unreadable input: block only if it mentions a game Data folder at all.
        reason = "unparseable hook input mentioning a Skyrim Data folder" if is_data_path(raw) else None
    except Exception as e:  # noqa: BLE001 - a crash would let the call through (exit code 1)
        reason = f"hook error ({type(e).__name__}: {e})" if mentions_game(raw) else None
    if reason:
        print(f"Blocked by .claude/hooks/block_game_data.py: {reason}. This project never writes into "
              "the game's Data folder (CLAUDE.md rule 3); build into dist/CardboardFaceMask/ and let "
              "the user install it.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
