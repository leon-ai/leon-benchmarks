# Publish the reports with GitHub Pages

This repository is a static site: the root `index.html` links to the dated report,
and the report's relative evidence links work under a project URL. No Node build,
model inference or benchmark execution is required. The existing `.nojekyll`
file prevents Jekyll processing.

## Before pushing

1. Verify the public inventory with `python3 harness/validation/verify.py`.
2. Review files being committed, including compressed traces. `.local/`, OAuth
   credentials and the raw private archive must remain untracked. The MIT notice
   contains the author's public name/email; synthetic benchmark credentials and
   public third-party information also appear in some records.
3. Commit the reviewed public files and push them to the `main` branch of
   `leon-ai/leon-benchmarks`. Repository creation, commits and pushes require the
   owner's explicit instruction; preparing this site performs none of them.

## Enable Pages

On GitHub, open `leon-ai/leon-benchmarks`:

1. Go to **Settings → Pages**.
2. Under **Build and deployment**, choose **Deploy from a branch**.
3. Choose branch **main** and folder **/(root)**, then **Save**.
4. Wait for the Pages deployment in **Actions**. The Pages settings show its URL.

With the organization/repository names above and no custom domain, the expected
landing-page URL is `https://leon-ai.github.io/leon-benchmarks/`. The dated report
is at `https://leon-ai.github.io/leon-benchmarks/wildclawbench/runs/2026-10-12/report/`.
These are expected URLs, not a claim that this repository has been deployed.

The local `.local/workspace` symlink is ignored and must not be pushed. GitHub's
branch publishing requires GitHub Actions instead if committed symbolic links
are present. Before enabling Pages, verify no symlinks were added to the tracked
publication. Pages exposes the deployed static records to visitors; use only the
reviewed sanitized export.

If a deployment fails, inspect the Pages workflow run. Keep all dated report and
companion record directories in the published source so their proof links work.
Future campaigns get their own run folders and a new landing-page link.

See the [official GitHub Pages publishing-source instructions](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site).

## Custom domain

The repository's `CNAME` declares `benchmarks.getleon.ai`. Configure that same
hostname in **Settings → Pages → Custom domain** before adding its DNS record.
The `getleon.ai` domain is verified for the `leon-ai` organization; retain its
verification TXT record.

At Namecheap, create a CNAME host record named `benchmarks` pointing to
`leon-ai.github.io`, with the default TTL. Preserve all existing root, `www`,
mail and verification records. Once GitHub confirms DNS and provisions its
certificate, enable **Enforce HTTPS**. DNS/certificate provisioning may take up
to 24 hours.

If GitHub adds or changes `CNAME` through repository settings, pull the commit
locally and review it before resealing the public inventory. Commit and push
updated checksum/validation controls so a clone still passes the offline verifier.

See [GitHub's custom-domain instructions](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site).
