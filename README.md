# Steady

A small, offline helper for neurodivergent people and people living with disability or mental health challenges. Prototype for the Mozilla Foundation Human-Scale AI Incubator.

It runs a small model **on your own computer**, keeps everything in a local file you can read, export or delete, and is built around specific needs rather than a general chatbot.

## What it does

- **Break it down**: turns a messy task into small physical steps, with a "good enough version". Works in a basic rule-based mode even with no model.
- **Say it plainly**: makes a message clearer or gentler, or gives a literal reading of a message you received (clearly flagged as a guess).
- **Check-in**: optional energy / overwhelm / sensory-load log. Saved only when you press Save. No streaks, no reminders, no gamification. Insights are plain averages only.
- **My data**: where the data is, a log of every request Steady has made, the exact instructions given to the model, one-click export, one-click delete.

## Quick start

Requires Python 3.9+ (no packages to install) and a local model server.

```bash
# 1. Install Ollama (https://ollama.com), then get a small model:
ollama pull llama3.2:3b        # ~2 GB. Any small instruct model works (3B-8B).

# 2. Start Steady
python3 server.py

# 3. Open http://127.0.0.1:8787
```

Using llama.cpp, LM Studio or anything else with an OpenAI-compatible `/v1/chat/completions` endpoint? Change `model_url` and `model_name` in `config.json`.

Run the tests (uses a fake model, no download needed): `python3 tests/test_e2e.py`

## How it maps to the Human-Scale AI principles

| Principle | In Steady |
|---|---|
| **Small** | 3B-class model on a laptop, no cloud, no GPU needed. Server is standard-library Python. |
| **Intentional** | Refuses to start with a non-local model unless you turn that on in config. Every outbound request is logged and visible. Prompts are plain text in `prompts.json` for the community to read, fork and govern. Export and delete anytime. Nothing is saved without an explicit Save. |
| **Tailored** | Literal, non-idiomatic language; no pressure words; sensory-load tracking; "good enough" endpoints for low-energy days; no streaks; text size and theme controls; no animations. |

## Safety design

- A built-in crisis check runs **before** any model call and does not depend on the model. It shows NZ resources (111, 1737, Lifeline, Suicide Crisis Helpline). Check these numbers occasionally, they can change.
- The model is instructed not to diagnose, give medical advice or act as a therapist.
- The server only listens on 127.0.0.1, checks the Host header, and requires a custom header on writes.

## Honest limitations

- **Keyword crisis detection is crude.** It will miss indirect language and can false-positive. It is a floor, not a safety system. This needs review with clinicians and lived-experience advisors before real use.
- **Small models make mistakes.** Outputs should be read before being relied on or sent.
- **No encryption at rest.** The SQLite file is plain. Use full-disk encryption (FileVault, BitLocker, LUKS).
- **Not co-designed yet.** The features are reasonable starting guesses. The Mozilla call asks for evidence of product-community fit with real users, so the next step is putting this in front of people in the community, changing it with them, and recording what they change.
- Not a medical device, not therapy, not a crisis service.

## Files

```
server.py          local server, model proxy, storage, privacy log
prompts.json       every instruction given to the model
config.json        port, model address, remote-model switch
static/index.html  the interface
tests/test_e2e.py  end-to-end tests with a fake model
data/              created on first run: your SQLite file (gitignored)
LICENSE            MIT
.gitignore         keeps data/ and local env files out of git
```

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 David Logan.
