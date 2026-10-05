# Teams page
verify: npm test
verify: npx tsc --noEmit
kind: feature

Shared context for every step. Follow the patterns in src/api/users.ts and
src/pages/Users.vue. Tests use vitest with the helpers in tests/setup.ts; mock
the API with msw as tests/api/users.test.ts does. Out of scope: styling,
routing changes, anything under src/auth/.

## step: api - Teams endpoint
scope: src/api/teams.ts, tests/api/teams.test.ts
context: src/api/users.ts, tests/api/users.test.ts
verify: npx vitest run tests/api
allow: npx vitest run

Add GET /api/teams returning { id, name, memberCount } for the current user's
teams, and GET /api/teams/:id with the members.

### Test cases
- user u1 in no teams: GET /api/teams -> 200, []
- u1 in team 3 "Core" with members u1, u2: GET /api/teams -> 200,
  [{ id: 3, name: "Core", memberCount: 2 }]
- team 7 without u1: GET /api/teams/7 -> 404, { error: "Team 7 not found" }
- u1 in team 3: GET /api/teams/3 -> 200,
  { id: 3, name: "Core", members: [{ id: "u1" }, { id: "u2" }] }

## step: store - Teams store
depends: api
scope: src/stores/teams.ts, tests/stores/teams.test.ts
context: src/stores/users.ts
verify: npx vitest run tests/stores

A Pinia store like src/stores/users.ts with load(), byId(id) and a loading flag,
using the client from the api step.

## step: page - Teams page
depends: store
scope: src/pages/Teams.vue, tests/pages/Teams.test.ts
context: src/pages/Users.vue
verify: npx vitest run tests/pages

A page listing the teams (name, member count) with the empty and loading states
from src/pages/Users.vue. No new styles.

## step: docs - API docs
scope: docs/api/teams.md
kind: docs

Document both endpoints in docs/api/teams.md, in the format of docs/api/users.md.
