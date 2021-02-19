# is-user-codeowner-action
Defines a docker container that can be used in Github Actions to determine whether a given github 
user is the owner of all paths included in a diff

```
on: pull-request
jobs:
  validate-ownership:
    runs-on: uwitiam/is-user-codeowner-action
    steps:
      - uses: actions/checkout@2
      - name: Is requester the CODEOWNER of this change?
        id: is-requester-codeowner
        run: -u ${GITHUB_ACTOR} -p ${GITHUB_WORKSPACE} -t main
```
