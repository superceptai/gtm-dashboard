# Dashboard data refresh — plain-English runbook

This dashboard (the page at **superceptai.github.io/gtm-dashboard/**) shows two
files that live in this repository:

- **`data.json`** — the numbers shown right now.
- **`history.jsonl`** — one snapshot per day, used for the week-over-week trends.

Every day at **6:00 pm Sydney time (AEST)** a GitHub Action wakes up, pulls fresh
numbers from four sources, rewrites `data.json`, adds one line to
`history.jsonl`, and saves them back to the repo. The website updates itself a
minute or two later. You can also run it by hand at any time (see below).

You never need a terminal or any code to operate this. Everything is a click in
the GitHub website.

---

## 1. The four secrets (and exactly where to get each one)

The Action needs four API keys to read the four sources. They are stored as
**GitHub repository secrets** — encrypted values only the Action can see. They
are **never** written into the code or shown in any log.

| Secret name (type it exactly) | What it reads | Where to get the value |
|---|---|---|
| `HUBSPOT_TOKEN` | ICP counts, seller bands, CEO/CRO contacts, beta pipeline | HubSpot → **Settings** (gear icon) → **Integrations → Private Apps** → open the read-only app (or **Create a private app**) → **Auth** tab → **Show token** → **Copy**. The app only needs *read* scopes: `crm.objects.contacts.read`, `crm.objects.companies.read`, `crm.objects.deals.read`, `crm.schemas.*.read`. |
| `REPLYIO_API_KEY` | Reply.io sequence stats (invites sent / accepted) | Reply.io → **Settings** → **API key** → **Copy** (or **Generate**). |
| `WINDSOR_API_KEY` | GA4 website + key-event numbers (via Windsor.ai) | Windsor.ai → sign in → **Account / API** page → copy your **API key**. |
| `CONNECTSAFELY_API_KEY` | LinkedIn follower count for Aaron's profile | ConnectSafely → **Settings / API** → copy your **API key**. |

### How to add or update a secret (one-time, ~2 minutes)

1. Go to the repository on GitHub: **`superceptai/gtm-dashboard`**.
2. Click **Settings** (top menu).
3. In the left sidebar: **Secrets and variables → Actions**.
4. Click **New repository secret**.
5. **Name** = the exact name from the table above (e.g. `HUBSPOT_TOKEN`).
   **Secret** = paste the value you copied.
6. Click **Add secret**.
7. Repeat for all four.

> The names must match exactly (capital letters, underscores). A typo means that
> one leg shows **NO DATA** on the dashboard.

---

## 2. How to run the refresh by hand (from the Actions tab)

Use this after adding the secrets the first time, or any time you want fresh
numbers immediately instead of waiting for 6 pm.

1. Go to the repo → **Actions** tab.
2. In the left sidebar click **Refresh dashboard data**.
3. Click the **Run workflow** button (right side).
4. Leave the tick-box unticked and click the green **Run workflow**.
5. Wait ~1 minute. Refresh the page — a new run appears. A **green tick** means it
   finished. Open the run and read the **Summary** box: it shows a ✅ or ❌ for
   each of the four sources.
6. Give the website another minute, then hard-refresh the dashboard.

**The tick-box ("Verify sources only")**: tick it if you just want to *test* that
all four keys work **without** changing the dashboard. In that mode the run will
go **red** if any source returns nothing — that is expected behaviour and tells
you which key needs attention. It does **not** commit any data.

---

## 3. How to read a red ("NO DATA") leg on the dashboard

At the top of the dashboard is a row of small **source health badges**:

> `HubSpot ICP · fresh · 2h ago`   `Reply.io · fresh · 2h ago`   `GA4 · fresh` …

- **Green "fresh"** = that source updated successfully. The "2h ago" is when it
  last succeeded.
- **Red "NO DATA"** = that source failed on the last run. The section of the
  dashboard fed by that source shows a **"No data"** box instead of numbers. This
  is deliberate — we would rather show nothing than show yesterday's number as if
  it were today's.

Importantly, **one red leg does not break the rest of the dashboard.** The other
three sections keep showing their normal, fresh numbers. Behind the scenes the
failed section keeps its previous value in `data.json` (so the history file is
never left with a hole), but the website hides it until the source recovers.

**What to do when you see red:**

1. Re-run the refresh by hand (Section 2). Sometimes a source is briefly down and
   the next run fixes it.
2. Still red? The key for that source has probably expired or been revoked —
   rotate it (Section 4).
3. For the **LinkedIn** leg specifically, there is a manual fallback so the
   follower number never goes blank — see Section 5.

---

## 4. How to rotate (replace) a key

Do this when a key stops working, or on a normal security rotation.

1. In the source tool (HubSpot / Reply.io / Windsor.ai / ConnectSafely),
   generate a **new** API key and copy it. (In HubSpot, rotate the Private App
   token from the app's **Auth** tab.)
2. In GitHub: **Settings → Secrets and variables → Actions**.
3. Click the secret's name → **Update** (or the pencil) → paste the new value →
   **Save**. The name stays the same.
4. Optionally revoke the old key in the source tool.
5. Run the refresh by hand (Section 2) to confirm the leg goes green again.

---

## 5. The manual-override file (LinkedIn followers + goal lines)

`config/manual_overrides.json` holds two human-editable things. Edit it in the
GitHub website (open the file → pencil icon → change the number → **Commit
changes**). No code needed.

- **`linkedin_followers`** — a *fallback* follower count. Normally the follower
  number comes live from ConnectSafely and this value is ignored. If ConnectSafely
  ever can't return it, the dashboard uses this number instead, so the tile is
  never blank. To update: open Aaron's LinkedIn profile, read the follower number,
  type it here (digits only, no commas), commit.
- **`funnel_targets`** — the goal line drawn on the funnel view (`connected` =
  target first-degree ICP connections; `intake` = target intake calls). Change the
  numbers to match the current quarter's goal.

---

## 6. What each source feeds (quick reference)

| Source (secret) | Dashboard sections |
|---|---|
| HubSpot (`HUBSPOT_TOKEN`) | ICP database, seller bands, ICP coverage, beta pipeline, funnel Track A |
| Reply.io (`REPLYIO_API_KEY`) | Outbound engine (sequences), funnel Track B |
| GA4 / Windsor.ai (`WINDSOR_API_KEY`) | Website — GA4 (sessions, key events) |
| ConnectSafely (`CONNECTSAFELY_API_KEY`) | LinkedIn followers |

---

## 7. Troubleshooting cheatsheet

| Symptom | Likely cause | Fix |
|---|---|---|
| One badge is red / "NO DATA" | That source's key expired or the source was down | Re-run by hand; if still red, rotate that key (Section 4) |
| All four badges red | Secrets not added yet, or names misspelled | Check the four secret names match Section 1 exactly |
| Dashboard shows an error banner instead of any data | `data.json` failed to load | Check the latest **Actions** run for a red X and open its logs |
| Followers tile shows an old number | ConnectSafely down, using manual fallback | Update `linkedin_followers` in `config/manual_overrides.json` |
| Manual "Verify sources only" run went red | One source returned empty (this is the check working) | See which leg failed in the run Summary, rotate that key |

The refresh runs on a schedule, so you can usually just wait for the next 6 pm
run. Running it by hand only ever *helps* — it never harms anything.
