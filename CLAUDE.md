### CLAUDE.md


## Software Design Principles
I want to use Hexagonal or ports and adaptor design pattern to make atlas. DRY and single use functions.


## TECH STACK
- Backend: fastapi backend + uv 
- Frontend: react+typescript + tailwindcss + redux state management
- Db adopter for - PostgreSQL, Snowflake
- No use of 'any' in backend and no assumptions on data anywhere
- make console-dev to startup frontend, make engine-dev to start backend
- use direnv to load up env variables in the venv
