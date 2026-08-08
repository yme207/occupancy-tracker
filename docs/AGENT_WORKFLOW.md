# Agent Development Workflow

How development sessions on this repo should be scoped, verified, and handed off — written so an
AI agent (Claude Code) can drive this project session over session without hallucinating APIs or
quietly losing context.

## Why this exists

Two failure modes this workflow is specifically designed to prevent:

1. **Hallucination** — inventing plausible-but-wrong Home Assistant APIs, as happened throughout
   this project's first prototype (see `docs/DECISIONS.md` for the record of what went wrong).
2. **Context collapse** — a session (or a chain of sessions) accumulating so much working context
   that it loses track of what's actually been verified vs. assumed, what's done vs. pending, or
   makes changes inconsistent with earlier decisions because it never re-grounded itself.

The countermeasure to both is the same: **keep sessions small and scoped, verify externally rather
than trust recall, and persist state to documentation rather than to conversation memory.**

## 1. Start-of-session ritual

Every session, before writing any code:

1. Read `docs/STATUS.md` in full. It is the source of truth for "where are we," not this
   conversation's history and not general recollection of the project.
2. Identify the *one* slice of work this session will complete (see §2). Do not start a session
   without a concrete, bounded target pulled from STATUS.md's next-steps list.
3. If the slice touches a Home Assistant API not already verified and recorded as such elsewhere
   in this repo, plan a verification step before implementation (§3).

## 2. Slice sizing

A "slice" is the unit of work for one session: implemented, tested, documented, done. Guidance:

- Sized to one architectural layer or one clearly-bounded feature from `docs/ARCHITECTURE.md`
  (e.g. "registry sync layer, read-only," or "provenance resolver + its unit tests" — not "the
  whole signal ingestion system").
- If a slice starts to sprawl across multiple layers, stop and split it — land what's done,
  update STATUS.md with the remainder as the next slice, rather than pushing on with a
  ballooning, harder-to-verify change.
- Prefer bottom-up order where possible (engine logic before the HA glue around it; HA glue before
  the frontend that depends on it) — see the phase order in `docs/STATUS.md`.

## 3. Verification-first for anything touching Home Assistant

Before writing code that calls into a Home Assistant API you haven't already verified in this
repo:

1. **Check first** whether it's already confirmed (a working, tested usage elsewhere in this repo,
   or a note in `docs/DECISIONS.md`).
2. **If not, verify it directly** — against the installed `homeassistant` package source (grep the
   actual class/method/constant), or current HA developer docs. A plausible-sounding name is not
   verification.
3. Only then implement against it.

For a non-trivial or unfamiliar area of the HA API (e.g. the `websocket_api` registration pattern,
`Context` chain walking, `Store` migrations, custom panel registration), treat the research step as
its own discrete piece of work — read the relevant HA source/docs, note the confirmed API shape
(in a code comment at the call site is fine — one line, not a treatise), *then* write the code
that depends on it in a separate step. Don't interleave "figuring out the API" with "writing
business logic" in a way that lets an unverified assumption slip through unflagged.

### Suggested use of sub-agents for this

This project is developed inside Claude Code, which has real sub-agent tooling (the `Agent` tool
with `Explore`/`general-purpose` agent types). Use it deliberately:

- **Research/verification passes** — spawn an `Explore` agent (or use `WebSearch`/reading the
  installed `homeassistant` package) to confirm an API before depending on it, especially for
  anything in the "genuinely unfamiliar" category above. This keeps speculative exploration out of
  the main session's context instead of bloating it with dead ends.
- **Broad codebase questions** ("where is X already handled," "does anything else already read
  this registry") — delegate to `Explore` rather than manually grep-crawling the whole repo inline.
- Keep the main thread focused on: read STATUS.md → implement the current slice → test → document.
  Push exploration and verification legwork into sub-agent calls so the main session's context
  stays proportional to the slice actually being built, not the entire investigation trail that
  led there.

## 4. Definition of done (per slice)

- Code implements exactly the scoped slice — no speculative extra scope "while I'm in here."
- Tests exist and **pass** (`docs/TESTING.md`), including for edge cases the slice's spec section
  explicitly calls out.
- `ruff`/`mypy` clean.
- No hardcoded values that `docs/ARCHITECTURE.md` says should flow through the typed config or
  come from a registry/topology store.
- `docs/STATUS.md` updated: mark the slice done, update "what's next."
- `docs/DECISIONS.md` gets a new entry if this slice made or changed a design decision (not just
  "implemented as specified" — only when something was actually decided).
- If the slice touched the UI, it's been checked against `docs/UX_GUIDELINES.md`.

## 5. Review pass

Before considering a slice fully closed, do a second pass over the diff (a fresh read, or a
dedicated review sub-agent call) checking specifically for:

- Any of the banned patterns in `docs/ARCHITECTURE.md` §3.
- Any HA API usage that wasn't actually verified (re-check, don't just trust the earlier pass).
- Dead code, unused imports, leftover debug logging.
- Whether anything here should have been made an extension point instead of a one-off.

## 6. Handoff between sessions

Session continuity comes from the documentation, not from conversation memory:

- `docs/STATUS.md` must always be accurate enough that a session starting cold (no memory of prior
  sessions) can pick up correctly from it alone.
- Don't leave a slice half-done without recording the exact remaining state in STATUS.md — "mostly
  working, needs X" is not sufficient; be specific about what's implemented, what's tested, and
  what isn't.
- If a session ends having discovered something that changes an earlier decision, update the
  relevant spec/architecture doc *and* log it in `docs/DECISIONS.md` — don't leave the correction
  only in that session's conversation.
