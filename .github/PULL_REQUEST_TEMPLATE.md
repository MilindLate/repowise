## Summary

<!-- What does this PR do? Keep it to 1-3 bullet points. -->

-

## Related Issues

<!-- Link any related issues: Fixes #123, Closes #456 -->

## Test Plan

<!-- How did you verify this works? -->

- [ ] Tests pass (`pytest`)
- [ ] Lint passes (`ruff check .`)
- [ ] Web build passes (`npm run build`) *(if frontend changes)*

## Precision

<!-- Required. Paste the compare table from
`python scripts/kg_validate/run.py --precision --ci fast --compare scripts/kg_validate/precision_baselines/`
(the `precision-fast` CI job also posts it as a comment). If the change can move
results on other repos, run the wider set (`--split dev`) and paste that instead.
No >2pp regression on any repo, and no fix that names a specific repo (rule R2). -->


## Checklist

- [ ] My code follows the project's code style
- [ ] I have added tests for new functionality
- [ ] All existing tests still pass
- [ ] I have updated documentation if needed
