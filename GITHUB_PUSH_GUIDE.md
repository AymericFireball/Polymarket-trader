# GitHub Push Guide — Polymarket Trading Agent

This guide walks you through pushing the Polymarket Trading Agent to GitHub from your local machine.

## Pre-Push Checklist

### 1. Evan's Changes Integration
- [ ] Get Evan's updated files from him:
  - [ ] `mirofish_wrapper.py` (rewritten with correct API endpoints)
  - [ ] `HANDOFF.md` (complete setup guide)
  - [ ] Any updates to `config.py`
  - [ ] Any updates to `README.md`
- [ ] Copy his files into your local repo, replacing originals
- [ ] Verify the new `mirofish_wrapper.py` has the correct API sequence:
  ```
  POST /api/graph/ontology/generate
  POST /api/graph/build
  POST /api/simulation/create
  POST /api/simulation/prepare
  POST /api/simulation/start
  GET  /api/simulation/{id}/run-status
  POST /api/simulation/interview/all
  POST /api/report/generate
  ```

### 2. Secrets & Credentials Check
- [ ] Verify `.env` file is NOT committed (should be in .gitignore)
- [ ] Verify `config.py` has NO real API keys filled in (all should be empty strings)
- [ ] Verify `.env.example` exists and serves as a template
- [ ] Run: `git status` and confirm no `.db` files, `.env`, or `secrets.py` appear

### 3. Files Ready to Commit

These files should exist in the repo:
```
✓ requirements.txt          (NEW — dependencies list)
✓ .env.example              (NEW — credentials template)
✓ .gitignore                (already exists)
✓ README.md                 (already exists, check Evan's updates)
✓ HANDOFF.md                (NEW from Evan)
✓ All Python source files (*.py)
✓ signals/ directory        (all 5 signal modules)
✓ dashboard.html            (browser monitoring interface)
✓ .git/                     (git metadata)
```

### 4. Create GitHub Repository

If you don't have one yet:

```bash
# Option A: Create via GitHub CLI (easiest)
gh repo create polymarket-trader \
  --private \
  --source=. \
  --push \
  --description="Autonomous prediction market trading agent for Polymarket"

# Option B: Manual
# 1. Go to https://github.com/new
# 2. Name: "polymarket-trader"
# 3. Description: "Autonomous prediction market trading agent for Polymarket"
# 4. Visibility: Private (or Public if you prefer)
# 5. Do NOT initialize with README/gitignore (we have them)
# 6. Create repo
# 7. Follow the "push existing repository" instructions
```

---

## Step-by-Step Push Process

### Step 1: Verify You're in the Right Directory
```bash
cd /path/to/Polymarket\ Trader
pwd
ls -la | grep -E "(run.py|config.py|.git|.gitignore)"
```

Expected output: Shows run.py, config.py, .git directory, and .gitignore

### Step 2: Check Current Git Status
```bash
git status
```

**Important checks:**
- If it says "fatal: not a git repository" → reinitialize:
  ```bash
  rm -rf .git
  git init
  git config user.email "your-email@example.com"
  git config user.name "Your Name"
  ```

- If status shows `.db` files or `.env` → they're not in .gitignore properly
  ```bash
  # Verify they're in .gitignore:
  grep "*.db" .gitignore
  grep ".env" .gitignore
  ```

- If status shows thousands of `__pycache__` files → missing .gitignore rules
  ```bash
  # Add to .gitignore if missing:
  echo "__pycache__/" >> .gitignore
  git add .gitignore
  git commit -m "Fix: add __pycache__ to gitignore"
  ```

### Step 3: Stage All Files
```bash
git add -A
```

### Step 4: Verify What Will Be Committed
```bash
git status --short
```

**Should see:** All Python files, README, HANDOFF.md, requirements.txt, .env.example, etc.

**Should NOT see:**
- ❌ `polymarket.db` or `*.db-wal`
- ❌ `.env` (only `.env.example`)
- ❌ `scan_results.json`, `market_data.json`
- ❌ `__pycache__/`, `.pyc` files
- ❌ `*.log` files

### Step 5: Review Diff (Optional but Recommended)
```bash
git diff --cached --stat
```

This shows you what's about to be committed, with file sizes.

### Step 6: Create Initial Commit

Use this commit message format (include the co-author):

```bash
git commit -m "feat: Initial Polymarket Trading Agent with 6-stage pipeline

- 6-stage probabilistic trading pipeline (scanner, signals, preprocessing, MiroFish, calibration, decision gate)
- 5 signal sources: sharp traders, news, base rate, cross-platform, sentiment
- Risk management: Half-Kelly sizing, drawdown tracking, position monitoring
- Execution engine: Polymarket CLOB API integration with iceberg order support
- Trade journal: Full position tracking, P&L reporting, post-mortem analysis
- Calibration: Brier score tracking, shrinkage (until 50 predictions), Platt scaling (30+), category stratification
- CLI: 12+ commands (scan, analyze, execute, monitor, positions, daily, calibration, etc.)
- Database: SQLite schema with 8 tables (markets, predictions, resolutions, trades, sharp_traders, etc.)

Co-Authored-By: Evan <evan@example.com>"
```

Replace `evan@example.com` with Evan's actual email.

### Step 7: Add Remote (If Not Already Set)
```bash
# Check if remote exists:
git remote -v

# If empty, add it:
git remote add origin https://github.com/YOUR-USERNAME/polymarket-trader.git

# Or if you used 'gh repo create', this is done automatically
```

### Step 8: Push to GitHub
```bash
git push -u origin main
# or if your branch is 'master':
git push -u origin master
```

You might be prompted for authentication. Follow GitHub's prompts or use a personal access token.

### Step 9: Verify on GitHub
- Go to https://github.com/YOUR-USERNAME/polymarket-trader
- Confirm all files are there
- Confirm no secrets appear in any file

---

## Post-Push: Verify & Document

### Add GitHub Topics (Optional but Nice)
```bash
gh repo edit YOUR-USERNAME/polymarket-trader \
  --add-topic prediction-markets \
  --add-topic trading-bot \
  --add-topic polymarket \
  --add-topic autonomous-agent
```

### Update README on GitHub
If you want to add a GitHub-specific section, add to README.md:

```markdown
## GitHub Repository

This is the authoritative source for the Polymarket Trading Agent. All development happens here.

**Latest Release:** See [Releases](https://github.com/YOUR-USERNAME/polymarket-trader/releases)

**Issues & Discussions:** [GitHub Issues](https://github.com/YOUR-USERNAME/polymarket-trader/issues)

**Contribution Guidelines:** See HANDOFF.md for detailed setup and architecture.
```

---

## Troubleshooting

### Problem: Git says "fatal: not a git repository"
**Solution:**
```bash
git init
git config user.email "your-email@example.com"
git config user.name "Your Name"
git add -A
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR-USERNAME/polymarket-trader.git
git push -u origin main
```

### Problem: `.db` files keep appearing in git status
**Solution:**
```bash
# Remove them from git tracking (don't delete local files):
git rm --cached *.db *.db-wal *.db-journal *.db-shm
git commit -m "Remove: exclude database files from version control"

# Verify .gitignore has these patterns:
cat .gitignore | grep "*.db"
```

### Problem: "Everything up-to-date" but GitHub shows nothing
**Solution:**
```bash
# Verify push succeeded:
git log --oneline

# Check remote:
git remote -v

# If you want to force-push (use carefully):
git push -u origin main --force
```

### Problem: Authentication fails
**Solutions:**
- Use GitHub CLI: `gh auth login`
- Or create a Personal Access Token:
  1. Go to GitHub → Settings → Developer settings → Personal access tokens
  2. Create token with `repo` and `workflow` scopes
  3. Use token as password when git prompts

---

## Final Verification Checklist

After pushing, verify:

- [ ] Repository appears on GitHub
- [ ] All 25+ Python files are present
- [ ] `.env.example` is there (but not `.env`)
- [ ] `requirements.txt` is there
- [ ] `HANDOFF.md` (Evan's guide) is there
- [ ] `README.md` is updated
- [ ] No `.db`, `.log`, or `.pyc` files in repo
- [ ] No sensitive data in any committed files
- [ ] All signal modules are in `signals/` directory
- [ ] Commit message includes proper attribution to Evan

---

## Next Steps After Push

1. **Share the repo URL** with Evan and any collaborators
2. **Create a Releases page** (GitHub Releases) with setup instructions
3. **Add GitHub Issues templates** for bug reports and feature requests
4. **Set up branch protection** if needed (Settings → Branches → Require reviews)
5. **Add CI/CD** (optional): GitHub Actions to run tests on every push

---

## Questions?

Refer to:
- `README.md` — Project overview & architecture
- `HANDOFF.md` — Evan's detailed setup guide
- Individual module docstrings — Each Python file has a `"""docstring"""` at the top

Good luck! 🚀
