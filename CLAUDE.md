# CLAUDE.md — `remote-comfy-gen-docker`

This repo builds the `hearmeman/comfyui-serverless:<tag>` Docker image that runs on RunPod serverless.

## What the image actually does at runtime

`start_script.sh` (baked in via `Dockerfile`) clones the runtime repo at every cold start:

```bash
RUNTIME_REPO_URL=<https url>     # required at deploy time
RUNTIME_REPO_REF=main            # ref to check out; defaults to main
```

It then `git reset --hard origin/$REF` and `exec`s `$RUNTIME_DIR/start.sh`. So runtime fixes ship by pushing to `main` of the runtime repo — no rebuild needed for runtime code. **Caveat:** RunPod's FlashBoot skips `start_script.sh`, so warm workers stay on whatever they last cloned until a true cold start.

Image-level changes (Dockerfile, baked scripts, base CUDA version) DO require a rebuild — see below.

## CircleCI project — managing builds

CircleCI is wired to this repo via CircleCI's standalone VCS (UUID-based), not the gh/ shortcut. Slugs look weird as a result.

### Project identifiers

```
Project name (in CircleCI UI):  remote-comfy-gen
GitHub repo:                    Hearmeman24/remote-comfy-gen-docker
Org UUID:                       10689717-6fce-4cb4-81cf-5ab21447f70f
Project UUID:                   f745cb5d-de2b-446c-aa2f-9c2f03852d4f
Project slug (v2 API):          circleci/10689717-6fce-4cb4-81cf-5ab21447f70f/f745cb5d-de2b-446c-aa2f-9c2f03852d4f
Project slug (in job responses, equivalent encoding):
                                circleci/32X8cuEZM12jEx8zikBAvi/XXzKFzYL5ujXPwE1Fwv2FQ
```

Both slug forms refer to the same project; either works in v2 API URLs.

### Trigger filter

`.circleci/config.yml` only runs `build_and_push` on tag pushes matching:

```yaml
filters:
  tags:
    only: /^v.*$/
  branches:
    ignore: /.*/
```

So **pushing to `main` does nothing.** To build a new image:

```bash
git tag -a v<N> -m "v<N> build" <commit>
git push origin v<N>
```

The Docker tag matches the git tag exactly (`hearmeman/comfyui-serverless:v<N>`).

### Local CLI (`circleci`)

Installed via `brew install circleci`. Auth: `circleci setup` writes a token to `~/.circleci/cli.yml`. The CLI itself is mostly for config validation and local job runs — for pipeline/workflow/job inspection use the REST API with the token from that file.

### Recipes

All recipes assume:

```bash
TOKEN=$(grep -E "^token" ~/.circleci/cli.yml | awk '{print $2}')
SLUG="circleci/10689717-6fce-4cb4-81cf-5ab21447f70f/f745cb5d-de2b-446c-aa2f-9c2f03852d4f"
```

**List recent pipelines:**

```bash
curl -s -H "Circle-Token: $TOKEN" "https://circleci.com/api/v2/project/$SLUG/pipeline" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); [print(f\"#{p['number']:4} {p['state']:10} {(p.get('vcs',{}).get('tag') or p.get('vcs',{}).get('branch') or '?'):30} {p['created_at'][:19]} id={p['id']}\") for p in d.get('items',[])[:10]]"
```

**Workflows + status for a pipeline:**

```bash
curl -s -H "Circle-Token: $TOKEN" "https://circleci.com/api/v2/pipeline/<PIPELINE_ID>/workflow" | python3 -m json.tool
```

**Jobs in a workflow:**

```bash
curl -s -H "Circle-Token: $TOKEN" "https://circleci.com/api/v2/workflow/<WORKFLOW_ID>/job" | python3 -m json.tool
```

**Steps for a failed job (v1.1, gives presigned output URLs):**

```bash
curl -s -H "Circle-Token: $TOKEN" "https://circleci.com/api/v1.1/project/$SLUG/<JOB_NUMBER>" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); [print(f\"{s['name']}: \" + str([{'status':a.get('status'),'exit_code':a.get('exit_code')} for a in s['actions']])) for s in d['steps']]"
```

**Fetch the actual log of a failing step:**

```bash
URL=$(curl -s -H "Circle-Token: $TOKEN" "https://circleci.com/api/v1.1/project/$SLUG/<JOB_NUMBER>" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); [print(a['output_url']) for s in d['steps'] if s['name']=='<STEP_NAME>' for a in s['actions']]")
curl -s "$URL" | python3 -c "import json,sys; [print(m.get('message','')) for m in json.load(sys.stdin)]"
```

**Rerun the failed workflow only (cheaper than re-tagging):**

```bash
curl -s -X POST -H "Circle-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"enable_ssh": false, "from_failed": true}' \
  "https://circleci.com/api/v2/workflow/<WORKFLOW_ID>/rerun"
```

**Rebuild a tag from scratch:** delete + re-push the tag, OR use the pipeline trigger API with `tag` set in the body.

```bash
# Re-push (simplest, also retriggers webhook):
git push origin :refs/tags/v<N>     # delete remote tag
git push origin v<N>                # re-push it
```

### Notify FlowBot

`config.yml` posts FlowBot messages on build start, success, and failure (`FLOWBOT_WEBHOOK_URL` env var on the project). If those notifications stop appearing, check the project env vars in CircleCI UI.

## CI debugging quick-flow

1. Pipeline failed? Get its `id` from the list-pipelines recipe.
2. Get the workflow + its status from `workflow` endpoint.
3. Get the job(s) and find the failed one.
4. Get the steps via v1.1 to find which step failed.
5. Fetch the step log via its presigned URL.
6. Fix code → push commit to main (won't trigger build) → delete tag → re-push tag (triggers build).

## Brain

Project-level brain lives in the sibling `remote_comfy_generator` repo at `docs/brain/`. Image-build/CI/Docker concerns aren't covered there yet — add an `infra/circleci.md` page if this CI workflow becomes load-bearing.
