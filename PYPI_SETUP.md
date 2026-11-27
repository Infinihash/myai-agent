# Publishing to PyPI

## One-time setup

1. **Create a PyPI account** at https://pypi.org/account/register/
   - Use org email or `jlvardon@gmail.com`
   - Recommended username: `myaitoken`

2. **Create an API token**
   - Go to https://pypi.org/manage/account/token/
   - Token name: `myai-agent-github-actions`
   - Scope: **Project** → `myai-agent` (after first upload; use Account scope for first upload)
   - Copy the token (starts with `pypi-`)

3. **Add the token to GitHub Secrets**
   - Go to https://github.com/Infinihash/myai-agent/settings/secrets/actions
   - New repository secret → Name: `PYPI_API_TOKEN` → Value: paste token

## Publishing a new release

```bash
# Bump version in pyproject.toml
# Commit: git commit -am "release: v2.0.1"
git tag v2.0.1
git push origin v2.0.1
```

GitHub Actions will automatically build and publish to PyPI.

## First publish (manual — before CI is set up)

```bash
python3 -m venv .venv
.venv/bin/pip install build twine
.venv/bin/python -m build
TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-... .venv/bin/twine upload dist/*
```

## After publishing

Users can install with:
```bash
pip install myai-agent
myai-agent install --wallet 0x...
```
