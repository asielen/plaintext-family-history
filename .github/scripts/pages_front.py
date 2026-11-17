"""Build the Pages front page from README.md (rendered by GitHub's own Markdown API).

Usage: python3 pages_front.py <rendered-readme.html> <out-index.html>

plaintext.family opens on the README, exactly as GitHub shows it; the example
site lives beside it under /example/. Relative links in the README point at
files in the repo, which the Pages site does not carry, so each one is
rewritten to the file's page on GitHub. The example-site link is already an
absolute plaintext.family URL and is left alone.
"""
import re
import sys

REPO = 'https://github.com/asielen/plaintext-family-history'

body = open(sys.argv[1], encoding='utf-8').read()


def absolute(m: re.Match) -> str:
    attr, url = m.group(1), m.group(2)
    if re.match(r'^(?:[a-z][a-z0-9+.-]*:|#|//)', url, re.I):
        return m.group(0)
    path = url.lstrip('./')
    base = f'{REPO}/raw/master/' if attr == 'src' else f'{REPO}/blob/master/'
    return f'{attr}="{base}{path}"'


body = re.sub(r'\b(href|src)="([^"]*)"', absolute, body)

page = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plaintext Family History</title>
<meta name="description" content="An operating spec for a durable, file-first family-history archive with an AI research assistant layered on top.">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.5.1/github-markdown.min.css">
<style>
  body {{ margin: 0; background: #ffffff; }}
  @media (prefers-color-scheme: dark) {{ body {{ background: #0d1117; }} }}
  .markdown-body {{ box-sizing: border-box; max-width: 980px; margin: 0 auto; padding: 32px 16px 64px; }}
  .repo-bar {{ max-width: 980px; margin: 0 auto; padding: 12px 16px 0; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  .repo-bar a {{ color: #0969da; }}
  @media (prefers-color-scheme: dark) {{ .repo-bar a {{ color: #4493f8; }} }}
</style>
</head>
<body>
<div class="repo-bar"><a href="{REPO}">View the repository on GitHub</a> · <a href="example/">Open the example site</a></div>
<article class="markdown-body">
{body}
</article>
</body>
</html>
'''
open(sys.argv[2], 'w', encoding='utf-8').write(page)
