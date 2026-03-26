# ecs-slack-notifications
Based on the AWS example for handling ECS events https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs_cwet_handling.html

An AWS Lambda function that listens to ECS CloudWatch Events and posts deployment status notifications to a Slack channel.

## Prerequisites

- Python 3.x
- AWS CLI configured with appropriate credentials
- A Slack workspace where you can create apps

## 1. Create a Slack App

1. Go to https://api.slack.com/apps and create a new app.
2. Under **OAuth & Permissions**, add the following bot token scopes:
   - `channels:read`
   - `chat:write`
3. Install the app to your workspace and copy the **Bot User OAuth Token** (`xoxb-...`).
4. Invite the bot to the channel you want notifications posted in (e.g. `/invite @your-bot`).

## Environment Variables

The Lambda functions read the following environment variables:

### `main.py` (notify function)

| Variable                         | Description                                                  |
| -------------------------------- | ------------------------------------------------------------ |
| `SLACK_API_TOKEN`                | Slack bot token (`xoxb-...`)                                 |
| `SLACK_CHANNEL`                  | Channel name to post notifications to                        |
| `INCLUDED_CLUSTERS`              | Comma-separated cluster names to monitor, or `"all"`         |
| `AWS_REGION`                     | AWS region for ECS/DynamoDB clients                          |
| `TABLE_TASK_STATE`               | DynamoDB table name for task state                           |
| `TABLE_TASK_DIGEST`              | DynamoDB table name for task digest                          |
| `TABLE_CONTAINER_INSTANCE_STATE` | DynamoDB table name for container instance state             |
| `DIGEST_ITEM_TTL`                | TTL in seconds for digest items (default: 2592000 / 30 days) |
| `STATE_ITEM_TTL`                 | TTL in seconds for state items (default: 86400 / 24h)        |

## Bulding Lambda Function

Build a zip and upload it manually to AWS Lambda:

```bash
pip install -r requirements.txt
./build_zip.sh
```

## Local Development

```bash
pip install -r requirements.txt

# Install and run pre-commit hooks (uses black for formatting)
pre-commit install
pre-commit run --all-files
```