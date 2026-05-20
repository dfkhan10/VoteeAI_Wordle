# Wordle Bot — Votee AI Engineer Interview

An entropy-based Wordle solver that plays against the Votee Wordle API
(`https://wordle.votee.dev:8000`). Built for the first-round coding test of the
Votee AI Engineer (Agents, Context & Evals) role.

The bot connects to the API, makes guesses, filters its candidate pool against
the response, and uses information theory to pick each subsequent guess. When
the answer turns out to be outside the bot's word list (which the Votee API
occasionally produces), it falls back to a two-phase letter-discovery +
constraint-enumeration brute-force.

---

## The Task

Votee provided an API that plays a Wordle-like puzzle. The task: write a program
that automatically solves these puzzles by calling the API.

The API exposes three endpoints — all return an array of
`{slot, guess, result}` where `result ∈ {absent, present, correct}`:

| Endpoint                                     | Purpose                                                  |
| -------------------------------------------- | -------------------------------------------------------- |
| `GET /random?guess=<word>&size=5&seed=<int>` | Guess against a seeded random word                       |
| `GET /daily?guess=<word>&size=5`             | Guess against today's daily word                         |
| `GET /word/{word}?guess=<word>`              | Guess against a specific known word (useful for testing) |

---

## Algorithm Overview

### Primary mode — entropy-based candidate filtering

1. **Word pool.** On startup, `load_all_words` builds the union of all 5-letter
   alphabetic words from every `.txt` file in the directory. The pool currently
   holds **20,231 unique words**.
2. **First guess: `SALET`.** Hardcoded to skip an expensive 20k-by-20k entropy
   computation on turn 1. `SALET` is widely considered the optimal opener by
   expected-information-gain analysis. (Previously used `CRANE`; switched after
   testing.)
3. **Filter.** After every guess, the candidate pool is filtered to words that
   would produce the observed pattern given the guess.
4. **Choose next guess.** Pick the word that maximises Shannon entropy over the
   remaining candidates — i.e., the word that splits the candidate set most
   evenly across possible pattern outcomes. The search pool is the candidates
   plus up to 300 non-candidate "sacrifice" words from the broader dictionary
   (a sacrifice can split the candidates more evenly than any candidate itself
   can, especially early in the game).
5. **Tiebreak.** Among equal-entropy words, prefer one that could itself be the
   answer (small score bump for candidates).
6. **Shortcut.** When `len(candidates) <= guesses_remaining`, just try the
   candidates sequentially — we're guaranteed to win without sacrificing info.

### The pattern-logic quirk

This particular Wordle implementation uses **simplified duplicate-letter
logic**: a "present" result means the letter exists anywhere in the answer,
regardless of whether another occurrence of that letter was already accounted
for by a "correct" match elsewhere. Standard Wordle would mark the second
occurrence "absent" once the first is matched — this API doesn't.

Example discovered during testing: guessing `PEERT` against the daily word
`PETRI` returns `(correct, correct, present, correct, present)`. The second
`E` in the guess is marked **present** even though the only `E` in `PETRI`
was already matched at position 1. Standard Wordle would mark it absent.

The bot's `compute_pattern` reflects this: it just checks "is this letter in
the answer's letter set?" — no per-letter accounting.

### Off-list fallback — two phases

The API occasionally chooses answers that aren't in our combined word list
(archaic words, proper-noun-like entries, etc.). When the candidate pool
filters down to zero, the bot doesn't give up — it switches strategies:

**Phase 1 — Probe for letter discovery.**
`choose_probe_guess` picks a real word from the dictionary that exposes the
maximum number of fresh (untested) letters while avoiding letters already known
to be absent. The probe word itself is unlikely to be the answer; its purpose
is to discover which letters the answer contains.

**Phase 2 — Constraint enumeration & brute force.**
`constraint_sequences` enumerates every 5-letter sequence (not restricted to
dictionary words — the API accepts arbitrary 5-letter input) consistent with
the accumulated constraints:

- `correct_at[pos]` — definite letter at definite position
- `present` — letters known to be in the answer somewhere
- `absent` — letters known not to be in the answer
- `not_at[letter]` — forbidden positions for a "present" letter

The bot enters phase 2 when the enumerated set is small enough (≤ 500
sequences), then runs entropy-based selection on it. If the set is still too
large, it falls back to phase 1 to gather more letter information first.

---

## Word Lists

The bot ingests every `.txt` file in the working directory and unions the
5-letter alphabetic entries. Multiple lists were added incrementally as we
discovered the Votee API draws from a broader vocabulary than any single list
covers:

| File                    | Entries (raw) | Notes                                          |
| ----------------------- | ------------- | ---------------------------------------------- |
| `possible_words.txt`    | 2,309         | Standard Wordle solution pool                  |
| `allowed_words.txt`     | 12,947        | Standard Wordle allowed-guess pool             |
| `both.txt`              | 12,971        | Combination of the above (provided by user)    |
| `long.txt`              | 10,656        | Extended list                                  |
| `short.txt`             | 2,314         | Smaller curated list                           |
| `stanford_words.txt`    | 5,756         | Stanford English word list                     |
| `wordle-dictionary.txt` | 14,856        | JSON-formatted dictionary, parsed as text      |
| `sowpods.txt`           | 267,751       | SOWPODS Scrabble dictionary (all word lengths) |
| `words_alpha.txt`       | 370,105       | English `words_alpha` corpus                   |

After deduplication and filtering to 5-letter alphabetic words: **20,231 unique
words.**

The loader strips JSON-formatting characters (`"`, `[`, `]`, `,`) so files like
the original `wordle-dictionary` (a concatenated JSON array) parse correctly
once renamed to `.txt`.

---

## Performance

Diagnostic on the 100-game `--eval` run with the smaller (13k) word pool
showed **all 58 failures were "answer not in our word lists"** — zero failures
were the algorithm running out of guesses when the word was in the pool. The
entropy search converges reliably whenever the answer is reachable.

Typical solve quality (when the answer is in the pool): **3–4 guesses
average**. Examples observed during development:

- Random `seed=42` → solved `WROTE` in 4 guesses (CRANE) / 4 with SALET path
- Daily (`PETRI`) → solved in 3 guesses with SALET (SALET → TRINE → PETRI)
- Direct word lookup (`/word/{word}`) with `SALET` opener → 3 guesses for PETRI

Expanding the pool (`possible_words.txt` → 20k unified pool) was the single
biggest coverage win for random-mode solving. The off-list fallback (probe +
brute force) further extends coverage to answers genuinely outside any
dictionary we have, though full position recovery in 6 guesses is not
guaranteed when the answer is truly outside the lists.

---

## File Structure

```
VoteeAI_Wordle/
├── wordle_bot.py              # The full bot — algorithm, API client, CLI
├── README.md                  # This file
├── openapi.json               # API spec from Votee
├── possible_words.txt         # ┐
├── allowed_words.txt          # │
├── both.txt                   # │
├── long.txt                   # │   All ingested into one 20k-word
├── short.txt                  # │   superset on startup
├── stanford_words.txt         # │
├── wordle-dictionary.txt      # │
├── sowpods.txt                # │
└── words_alpha.txt            # ┘
```

The bot source has four logical sections:

- **Word loading** — `load_all_words` (globs `.txt`, strips JSON noise, filters
  to 5-letter alpha).
- **Core Wordle logic** — `compute_pattern` (the API's simplified rule),
  `filter_candidates`, `entropy_of_guess`, `choose_guess`,
  `choose_probe_guess`, `compute_constraints`, `constraint_sequences`.
- **API client** — `WordleAPI` wraps the three endpoints with a 15s timeout.
- **Bot** — `WordleBot` orchestrates a game; `.solve(...)` runs one game,
  `.evaluate(n_games=100)` runs the diagnostic and prints failure breakdown.

---

## Usage

The bot has no external dependencies beyond `requests`. Run directly:

```bash
python3 wordle_bot.py
```

This executes the three demo games at the bottom of `wordle_bot.py`:

- One seeded random puzzle
- The current daily puzzle
- A `/word/{word}` lookup against `petri`

To run a 100-game diagnostic with per-failure breakdown:

```bash
python3 wordle_bot.py --eval
```

To use the bot programmatically:

```python
from wordle_bot import WordleBot

bot = WordleBot()

# Solve today's daily puzzle
num_guesses, history = bot.solve(mode="daily", verbose=True)

# Solve a reproducible random puzzle
bot.solve(mode="random", seed=42, verbose=True)

# Solve against a specific target (useful for testing the algorithm)
bot.solve(mode="word", target="petri", verbose=True)
```

---

## Development Timeline

This document is the record of decisions made building the bot:

1. **Initial implementation** — Entropy-based bot using `possible_words.txt`
   (2,309 candidates) with `CRANE` opener. Worked, but 26% of random seeds
   failed because the API's words extended beyond the standard solution pool.

2. **Fallback to `allowed_words.txt`** — Added a fallback that re-derived
   candidates from a broader list when the candidate pool emptied. Improved
   coverage marginally.

3. **Unified word pool** — Replaced separate primary + fallback lists with a
   single union of all `.txt` files. This eliminated the fallback indirection
   entirely. Pool grew from 13,112 → 14,947 → 20,231 as more lists were added
   (wordle-dictionary, sowpods, words_alpha).

4. **Discovered the API quirk** — When the bot proposed `PEERT` against daily
   `PETRI`, the API returned a pattern that no standard-Wordle implementation
   would have produced. Tracing through revealed the API marks "present" for
   any letter in the answer, even one already matched by an earlier "correct"
   — no letter-count tracking. `compute_pattern` was simplified to match.

5. **Off-list fallback** — Some daily answers (e.g., archaic words like
   `PETRE`) are outside every dictionary. Instead of giving up when candidates
   exhaust, the bot now (a) probes with fresh-letter words to discover what
   letters the answer contains, then (b) brute-force-enumerates 5-letter
   sequences consistent with all constraints.

6. **Opener change** — Switched from `CRANE` (5 high-frequency letters, good
   coverage) to `SALET` (provably optimal opener by entropy across the
   standard solution set). Measurably better solve length on the test cases.
