# ecs-slack-notifications
Based on the AWS example for handling ECS events https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs_cwet_handling.html

# Installation
1. Requires changes from https://github.com/serverless/serverless/pull/4694 which
will be released with version 1.27. Until then just install from master.
```bash
npm install -g serverless/serverless#master
```

2. Install python requirements serverless plugin in the app directory.
```bash
npm install serverless-python-requirements
```

3. Create a Slack application and copy the token. The app needs the following permission scopes:
- `chat:write`

4. Create `.env.yml` in the app directory
```bash
$ cat .env.yml
notify:
  environment:
    SLACK_API_TOKEN: "xoxa-11111111111-1111111111111-1111111111111-abcd3abcd3abcd3abcd3abcd3abcd3123"
    SLACK_CHANNEL: "ecs-notifications"
    INCLUDED_CLUSTERS: "production,staging"
```

5. Install the app on aws
```bash
sls deploy
```