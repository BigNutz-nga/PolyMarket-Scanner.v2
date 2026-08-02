# Publish to a non-expiring GitHub repository

This project archive is Git-ready, but creating a repository requires authorization to your GitHub account. From this directory:

```bash
git init
git add .
git commit -m "Initial Polymarket-Scanner v2"
git branch -M main
```

Using GitHub CLI:

```bash
gh auth login
gh repo create polymarket-scanner --private --source=. --remote=origin --push
```

Or create an empty repository in the GitHub web UI and run:

```bash
git remote add origin git@github.com:YOUR_GITHUB_USER/polymarket-scanner.git
git push -u origin main
```

Your resulting URL will be:

```text
https://github.com/YOUR_GITHUB_USER/polymarket-scanner
```

That URL is controlled by your account and does not depend on a temporary file sandbox.
