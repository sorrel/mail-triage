"use strict";

/* Ranking folder names against what you have typed.
 *
 * Kept in its own file for one reason: it is the only real logic in the
 * page, and here it can be tested (tests/test_web_fuzzy.py drives it through
 * node) rather than eyeballed in a browser.
 *
 * A match is a subsequence — "hw" finds "Home/Web" — because that is what
 * makes a deep folder tree reachable in three or four keystrokes. Scoring
 * then decides which of the matches you actually meant:
 *
 * - a character starting a path segment or a word is worth far more than one
 *   in the middle, so "hw" prefers "Home/Web" over "Show/answer"
 * - consecutive characters compound, so typing more of a real name wins
 * - matching within the leaf beats matching a parent, because the leaf is
 *   what people call the folder
 * - shorter names break ties, so "Work" beats "Work/Work Tech/Newsletters"
 *   when both match equally well
 */

const SEPARATORS = "/ -_.";

function scoreFolder(query, folder) {
  const wanted = query.toLowerCase();
  const name = folder.toLowerCase();
  let index = 0;
  let total = 0;
  let streak = 0;
  let previous = -2;
  let firstHit = -1;

  for (let at = 0; at < name.length && index < wanted.length; at += 1) {
    if (name[at] !== wanted[index]) continue;
    let value = 1;
    if (at === 0 || SEPARATORS.includes(name[at - 1])) value += 4;
    if (previous === at - 1) {
      streak += 1;
      value += streak;
    } else {
      streak = 0;
    }
    if (firstHit < 0) firstHit = at;
    total += value;
    previous = at;
    index += 1;
  }

  if (index < wanted.length) return -1;

  const leafAt = name.lastIndexOf("/") + 1;
  if (firstHit >= leafAt) total += 3;
  // Prefer the shorter of two equally good names, gently enough that it
  // never outweighs a genuinely better match.
  total -= (name.length - wanted.length) * 0.05;
  return total;
}

/* Ranked matches, best first.
 *
 * With nothing typed the expected folder leads — so opening the picker and
 * pressing Return files where the tool already thought it should go, and
 * typing is only needed to disagree.
 */
function rankFolders(query, folders, expected = null) {
  const all = [...folders];
  if (!query) {
    const rest = all.filter((name) => name !== expected);
    return expected && all.includes(expected) ? [expected, ...rest] : rest;
  }
  return all
    .map((name) => ({ name, score: scoreFolder(query, name) }))
    .filter((entry) => entry.score >= 0)
    .sort(
      (a, b) =>
        b.score - a.score ||
        a.name.length - b.name.length ||
        a.name.localeCompare(b.name)
    )
    .map((entry) => entry.name);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { rankFolders, scoreFolder };
} else {
  globalThis.rankFolders = rankFolders;
  globalThis.scoreFolder = scoreFolder;
}
