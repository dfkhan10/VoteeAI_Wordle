#!/usr/bin/env python3
"""
Wordle Bot — solves Wordle puzzles via the Votee API using entropy-based word selection.

Strategy:
  1. Build a single candidate pool from the union of all .txt word files in the directory.
  2. Each turn, pick the guess that maximises expected information gain (Shannon entropy)
     over the remaining candidates.
  3. Non-candidate words are considered when guesses_remaining > 1 — a sacrifice guess
     that splits the pool better than any candidate is worth playing.
  4. After receiving feedback, filter candidates to only those consistent with the result.

API: https://wordle.votee.dev:8000
  GET /random?guess=<word>&size=5[&seed=<int>]
  GET /daily?guess=<word>&size=5
  GET /word/{word}?guess=<word>

  Response: [{slot, guess, result}] where result ∈ {absent, present, correct}
"""

import glob
import json
import math
import sys
import requests
from collections import defaultdict
from typing import Optional


BASE_URL = "https://wordle.votee.dev:8000"

# Hardcoded first guess — avoids computing entropy over the full word pool on turn 1.
# "salet" is widely considered the optimal opener by expected-info-gain analysis.
STARTING_WORD = "salet"


# ---------------------------------------------------------------------------
# Word loading
# ---------------------------------------------------------------------------

def load_all_words(directory: str = ".") -> list[str]:
    """Union of all 5-letter words from every .txt file and JSON word list in `directory`."""
    words: set[str] = set()

    # Plain-text files: one word per line (also handles JSON-formatted lines)
    for path in glob.glob(f"{directory}/*.txt"):
        with open(path) as f:
            for line in f:
                w = line.strip().lower().strip('"[],')
                if len(w) == 5 and w.isalpha():
                    words.add(w)

    # JSON array files (e.g. wordle-dictionary)
    for path in glob.glob(f"{directory}/*"):
        if path.endswith((".txt", ".py", ".json", ".pdf", ".pyc")):
            continue
        import os
        if os.path.isdir(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, list):
                for w in data:
                    w = str(w).strip().lower()
                    if len(w) == 5 and w.isalpha():
                        words.add(w)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    return sorted(words)


# ---------------------------------------------------------------------------
# Core Wordle logic
# ---------------------------------------------------------------------------

def compute_pattern(guess: str, answer: str) -> tuple[str, ...]:
    """Return the 5-element colour pattern for a guess against a known answer.

    This API uses simplified duplicate-letter logic: "present" means the letter
    exists anywhere in the answer, even if already accounted for by a "correct"
    match elsewhere — no letter-count tracking.
    """
    answer_set = set(answer)
    pattern = []
    for i in range(5):
        if guess[i] == answer[i]:
            pattern.append("correct")
        elif guess[i] in answer_set:
            pattern.append("present")
        else:
            pattern.append("absent")
    return tuple(pattern)


def filter_candidates(candidates: list[str], guess: str, pattern: tuple[str, ...]) -> list[str]:
    """Keep only candidates whose pattern against `guess` matches the observed result."""
    return [w for w in candidates if compute_pattern(guess, w) == pattern]


def entropy_of_guess(guess: str, candidates: list[str]) -> float:
    """Expected bits of information gained by playing `guess` given remaining `candidates`."""
    counts: dict[tuple, int] = defaultdict(int)
    for word in candidates:
        counts[compute_pattern(guess, word)] += 1
    n = len(candidates)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def choose_guess(candidates: list[str], all_words: list[str], guesses_remaining: int) -> str:
    """Return the best next guess."""
    n = len(candidates)

    if n == 1:
        return candidates[0]

    # Enough guesses to try every remaining candidate — no sacrifice needed.
    if n <= guesses_remaining:
        return candidates[0]

    # Score candidates plus a sample of non-candidates.
    # Non-candidates can't win directly but may split the pool more evenly.
    candidate_set = set(candidates)
    search_pool = candidates.copy()
    if guesses_remaining > 1:
        non_candidates = [w for w in all_words if w not in candidate_set]
        search_pool.extend(non_candidates[:300])

    best_word, best_score = candidates[0], -1.0
    for word in search_pool:
        score = entropy_of_guess(word, candidates)
        if word in candidate_set:
            score += 0.001  # tiebreak: prefer a word that could itself be the answer
        if score > best_score:
            best_score, best_word = score, word

    return best_word


def choose_probe_guess(all_words: list[str], history: list[tuple[str, tuple]]) -> str:
    """Phase 1 of the off-list fallback: pick a real word that exposes the maximum
    number of fresh (untested) letters while avoiding letters known to be absent.

    The probe word itself is unlikely to be the answer — its purpose is to discover
    which letters the answer contains. Once enough letters are known, phase 2
    enumerates the small set of letter arrangements that match all constraints.
    """
    tested: set[str] = set()
    absent: set[str] = set()
    for guess, pattern in history:
        for i, p in enumerate(pattern):
            ch = guess[i]
            tested.add(ch)
            if p == "absent":
                absent.add(ch)

    best_word = all_words[0]
    best_score = (-1, 1)  # (fresh_letter_count, -absent_letter_count)

    for word in all_words:
        letters = set(word)
        fresh = len(letters - tested)
        bad = len(letters & absent)
        score = (fresh, -bad)
        if score > best_score:
            best_score = score
            best_word = word

    return best_word


def compute_constraints(history: list[tuple[str, tuple]]):
    """Extract Wordle constraints from guess history.

    Returns:
        correct_at: dict pos -> letter (definite letter at definite position)
        present:    set of letters known to be in the answer somewhere
        absent:     set of letters known NOT to be in the answer at all
        not_at:     dict letter -> set of forbidden positions (from "present"
                    results placed at those positions)
    """
    correct_at: dict[int, str] = {}
    present: set[str] = set()
    absent: set[str] = set()
    not_at: dict[str, set[int]] = defaultdict(set)

    for guess, pattern in history:
        for i, p in enumerate(pattern):
            ch = guess[i]
            if p == "correct":
                correct_at[i] = ch
                present.add(ch)
            elif p == "present":
                present.add(ch)
                not_at[ch].add(i)
            else:  # absent
                absent.add(ch)

    # A letter can't be both present and absent — present wins.
    absent -= present
    return correct_at, present, absent, not_at


def constraint_sequences(history: list[tuple[str, tuple]], max_count: int = 3000) -> list[str]:
    """Phase 2: enumerate all 5-letter sequences consistent with the constraints.

    These are not restricted to dictionary words — the API accepts any 5-letter
    string and returns a pattern, so any constraint-consistent arrangement is a
    valid guess and one of them is the actual answer (by definition).

    Stops early once `max_count` sequences are found.
    """
    correct_at, present, absent, not_at = compute_constraints(history)
    alphabet = set("abcdefghijklmnopqrstuvwxyz")

    # Letters allowed at each position
    position_letters: list[list[str]] = []
    for pos in range(5):
        if pos in correct_at:
            position_letters.append([correct_at[pos]])
        else:
            allowed = sorted(
                l for l in alphabet - absent if pos not in not_at.get(l, set())
            )
            position_letters.append(allowed)

    results: list[str] = []

    def backtrack(pos: int, current: list[str], remaining_present: set[str]) -> None:
        if len(results) >= max_count:
            return
        # Pruning: not enough remaining positions to fit all present letters
        if 5 - pos < len(remaining_present):
            return
        if pos == 5:
            if not remaining_present:
                results.append("".join(current))
            return
        for letter in position_letters[pos]:
            current.append(letter)
            backtrack(pos + 1, current, remaining_present - {letter})
            current.pop()
            if len(results) >= max_count:
                return

    backtrack(0, [], present)
    return results


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class WordleAPI:
    def _get(self, url: str, params: dict) -> list[dict]:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def guess_random(self, guess: str, seed: Optional[int] = None) -> list[dict]:
        params: dict = {"guess": guess, "size": 5}
        if seed is not None:
            params["seed"] = seed
        return self._get(f"{BASE_URL}/random", params)

    def guess_daily(self, guess: str) -> list[dict]:
        return self._get(f"{BASE_URL}/daily", {"guess": guess, "size": 5})

    def guess_word(self, target: str, guess: str) -> list[dict]:
        return self._get(f"{BASE_URL}/word/{target}", {"guess": guess})


def parse_response(data: list[dict]) -> tuple[str, ...]:
    """Convert API response into a pattern tuple ordered by slot."""
    return tuple(item["result"] for item in sorted(data, key=lambda x: x["slot"]))


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

TILE = {"correct": "🟩", "present": "🟨", "absent": "⬛"}


class WordleBot:
    def __init__(self, word_dir: str = "."):
        self.words = load_all_words(word_dir)
        self.api = WordleAPI()
        print(f"Loaded {len(self.words)} unique words from {word_dir}/*.txt")

    def solve(
        self,
        mode: str = "random",
        seed: Optional[int] = None,
        target: Optional[str] = None,
        max_guesses: int = 6,
        verbose: bool = True,
    ) -> tuple[int, list[tuple[str, tuple]]]:
        """
        Play one complete game.

        Args:
            mode:        "random" | "daily" | "word"
            seed:        seed for /random (makes result reproducible)
            target:      target word for mode="word"
            max_guesses: typically 6
            verbose:     print turn-by-turn output

        Returns:
            (num_guesses, history) — num_guesses is -1 on failure.
            history is a list of (guess, pattern) tuples.
        """
        candidates = self.words.copy()
        history: list[tuple[str, tuple]] = []

        def call_api(guess: str) -> list[dict]:
            if mode == "random":
                return self.api.guess_random(guess, seed=seed)
            if mode == "daily":
                return self.api.guess_daily(guess)
            if mode == "word":
                assert target, "target must be set for mode='word'"
                return self.api.guess_word(target, guess)
            raise ValueError(f"Unknown mode: {mode}")

        if verbose:
            label = f"seed={seed}" if mode == "random" else (target if mode == "word" else "daily")
            print(f"\n{'='*40}")
            print(f"  Mode: {mode}  |  {label}")
            print(f"{'='*40}")

        in_probe_mode = False

        for attempt in range(1, max_guesses + 1):
            guesses_remaining = max_guesses - attempt + 1

            mode_tag = ""
            if attempt == 1:
                guess = STARTING_WORD
            elif candidates:
                guess = choose_guess(candidates, self.words, guesses_remaining)
            else:
                # Word-list exhausted. Phase 2: enumerate constraint-consistent
                # sequences and treat them as the candidate pool. If too many,
                # fall back to phase 1 (probe for more letter info).
                constructed = constraint_sequences(history, max_count=2000)
                if 0 < len(constructed) <= 500:
                    guess = choose_guess(constructed, self.words, guesses_remaining)
                    mode_tag = f"  [brute-force, {len(constructed)} seqs]"
                else:
                    guess = choose_probe_guess(self.words, history)
                    seq_count = len(constructed) if constructed else 0
                    mode_tag = f"  [probe, {seq_count}+ seqs]"

            if verbose:
                print(f"\nGuess {attempt}/{max_guesses}: {guess.upper():<8}  [{len(candidates)} candidates]{mode_tag}")

            response = call_api(guess)
            pattern = parse_response(response)
            history.append((guess, pattern))

            if verbose:
                tiles = "".join(TILE[r] for r in pattern)
                letters = " ".join(
                    item["guess"].upper()
                    for item in sorted(response, key=lambda x: x["slot"])
                )
                print(f"           {letters}")
                print(f"           {tiles}")

            if all(r == "correct" for r in pattern):
                if verbose:
                    print(f"\n✅  Solved in {attempt} guess{'es' if attempt > 1 else ''}!")
                return attempt, history

            had_candidates = bool(candidates)
            if candidates:
                candidates = filter_candidates(candidates, guess, pattern)

            if had_candidates and not candidates:
                if verbose:
                    print("   ↩  Word lists exhausted — switching to probe mode.")
                in_probe_mode = True

            if verbose and candidates and len(candidates) <= 10:
                print(f"           Remaining: {[w.upper() for w in candidates]}")

        if verbose:
            tail = " in probe mode" if in_probe_mode else ""
            print(f"\n❌  Failed after {max_guesses} guesses{tail}.")
        return -1, history

    def evaluate(self, n_games: int = 100, verbose: bool = False) -> dict:
        """Run `n_games` with seeds 0..n_games-1 and report statistics."""
        print(f"\nEvaluating over {n_games} games...")
        results: list[int] = []
        failures: list[tuple[int, str, list]] = []  # (seed, reason, history)

        for seed in range(n_games):
            num_guesses, history = self.solve(mode="random", seed=seed, verbose=verbose)
            results.append(num_guesses)
            if num_guesses == -1:
                # Replay the filter to determine whether we ever entered probe mode.
                cands = self.words
                probe_started_at: Optional[int] = None
                for i, (g, p) in enumerate(history, start=1):
                    cands = filter_candidates(cands, g, p)
                    if not cands and probe_started_at is None:
                        probe_started_at = i
                if probe_started_at is not None:
                    reason = f"word not in our word lists (entered probe mode after guess {probe_started_at})"
                else:
                    reason = "ran out of guesses (word IS in pool)"
                failures.append((seed, reason, history))
            if (seed + 1) % 20 == 0:
                solved_so_far = sum(1 for r in results if r > 0)
                print(f"  [{seed+1}/{n_games}]  solved {solved_so_far}/{len(results)}")

        solved = [r for r in results if r > 0]
        stats = {
            "total": n_games,
            "solved": len(solved),
            "failed": n_games - len(solved),
            "solve_rate": len(solved) / n_games,
            "avg_guesses": sum(solved) / len(solved) if solved else 0,
            "distribution": {i: solved.count(i) for i in range(1, 7)},
        }

        print(f"\n{'='*40}")
        print(f"  Results over {n_games} games")
        print(f"{'='*40}")
        print(f"  Solved:        {stats['solved']}/{n_games} ({stats['solve_rate']:.1%})")
        print(f"  Avg guesses:   {stats['avg_guesses']:.2f}")
        print(f"  Distribution:  {stats['distribution']}")

        if failures:
            print(f"\n  Failures ({len(failures)}):")
            for seed, reason, history in failures:
                guesses = " → ".join(f"{g.upper()}({''.join(r[0].upper() for r in p)})" for g, p in history)
                print(f"    seed={seed:>3}  {reason}")
                print(f"             {guesses}")

        return stats


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    bot = WordleBot()

    bot.solve(mode="random", seed=42, verbose=True)
    bot.solve(mode="daily", verbose=True)
    bot.solve(mode="word", target="crane", verbose=True)

    if "--eval" in sys.argv:
        bot.evaluate(n_games=100)
