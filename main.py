import json
import boto3
import os
import time

from botocore.exceptions import ClientError
from collections import Counter
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

slack_token = os.environ["SLACK_API_TOKEN"]
channel = os.environ["SLACK_CHANNEL"]
included_clusters = os.environ["INCLUDED_CLUSTERS"]
region = os.environ["AWS_REGION"]
digest_item_ttl = os.getenv("DIGEST_ITEM_TTL", 2592000)
state_item_ttl = os.getenv("STATE_ITEM_TTL", 86400)
slack_ts_timeout = os.getenv("SLACK_TS_TIMEOUT", 600)
slack_group_size = int(os.getenv("SLACK_GROUP_SIZE", 50))
table_task_state = os.environ["TABLE_TASK_STATE"]
table_task_digest = os.environ["TABLE_TASK_DIGEST"]
table_container_instance_state = os.environ["TABLE_CONTAINER_INSTANCE_STATE"]

wc = WebClient(token=slack_token)

# Cache for channel ID to avoid repeated API calls
_channel_id_cache = {}


def get_slack_channels():
    """Fetch all public Slack channels with pagination support."""
    channels = []
    cursor = None

    try:
        while True:
            response = wc.conversations_list(
                exclude_archived=True, 
                cursor=cursor, 
                types="public_channel",
                limit=200  # Explicitly set limit for better performance
            )
            
            channels.extend(response.get("channels", []))
            
            # Check for next cursor
            cursor = response.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
                
    except SlackApiError as e:
        print(f"Error fetching Slack channels: {e.response['error']}")
        raise

    return channels


def get_slack_channel_id(name):
    """
    Get Slack channel ID by name with caching.
    Returns the channel ID or None if not found.
    """
    # Check cache first
    if name in _channel_id_cache:
        return _channel_id_cache[name]
    
    # If channel name starts with #, remove it
    channel_name = name.lstrip('#')
    
    try:
        channels = get_slack_channels()
        
        for c in channels:
            if c["name"] == channel_name:
                channel_id = c["id"]
                # Cache the result
                _channel_id_cache[name] = channel_id
                return channel_id
        
        print(f"Warning: Channel '{channel_name}' not found")
        return None
        
    except SlackApiError as e:
        print(f"Error looking up channel ID: {e.response['error']}")
        # Fall back to using the channel name directly
        # Slack API accepts channel names prefixed with #
        return f"#{channel_name}"


def lambda_handler(event, context):
    id_name = ""
    new_record = {}

    # For debugging so you can see raw event format.
    print("Here is the event:")
    print((json.dumps(event)))

    if event["source"] != "aws.ecs":
        raise ValueError(
            "Function only supports input from events with a source type of: aws.ecs"
        )

    # Switch on task/container events.
    table_name = ""
    if event["detail-type"] == "ECS Task State Change":
        table_name = table_task_state
        id_name = "taskArn"
        event_id = event["detail"]["taskArn"]
    elif event["detail-type"] == "ECS Container Instance State Change":
        table_name = table_container_instance_state
        id_name = "containerInstanceArn"
        event_id = event["detail"]["containerInstanceArn"]
    else:
        raise ValueError(
            "detail-type for event is not a supported type. Exiting without saving event."
        )

    new_record["cw_version"] = event["version"]
    new_record.update(event["detail"])

    # "status" is a reserved word in DDB, but it appears in containerPort
    # state change messages.
    if "status" in event:
        new_record["current_status"] = event["status"]
        new_record.pop("status")

    # Look first to see if you have received a newer version of an event ID.
    # If the version is OLDER than what you have on file, do not process it.
    # Otherwise, update the associated record with this latest information.
    print("Looking for recent event with same ID...")
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)
    saved_event = table.get_item(Key={id_name: event_id})
    if "Item" in saved_event:
        # Compare events and reconcile.
        print(("EXISTING EVENT DETECTED: Id " + event_id + " - reconciling"))
        if saved_event["Item"]["version"] < event["detail"]["version"]:
            print("Received event is more recent version than stored event - updating")
            ttl_value = int(time.time()) + int(state_item_ttl)
            new_record["TTL"] = ttl_value
            table.put_item(Item=new_record)
            if event["detail-type"] == "ECS Task State Change":
                update_task_digest(event)
        else:
            print("Received event is more recent version than stored event - ignoring")
    else:
        print(("Saving new event - ID " + event_id))
        if event["detail-type"] == "ECS Task State Change":
            update_task_digest(event)

        table.put_item(Item=new_record)


def chunk_list(items, size):
    return [items[i : i + size] for i in range(0, len(items), size)]


def assign_task_to_group(item, task_id):
    """
    Assign a task ID to a group of at most slack_group_size tasks, appending
    to the most recent group while it has room and only opening a new group
    once it is full. Existing membership is preserved across calls so a task
    always updates the same Slack message it was first reported in.
    """
    groups = item.get("task_groups")
    if not groups:
        # Digest predates grouping support - bucket everything seen so far.
        item["task_groups"] = chunk_list(list(item["tasks"].keys()), slack_group_size)
        return

    if any(task_id in g for g in groups):
        return

    if groups and len(groups[-1]) < slack_group_size:
        groups[-1].append(task_id)
    else:
        groups.append([task_id])


def update_task_digest(event):
    table_name = table_task_digest
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)
    event_detail = event["detail"]
    event_id = event_detail["startedBy"]
    task_id = event_detail["taskArn"].split("/")[-1]

    saved_event = get_dynamo_item(table, "startedBy", event_id)

    update_slack = True
    if "Item" in saved_event:
        item = saved_event["Item"]
        # if 'slack_ts' in item:
        #     if float(item['slack_ts']) < float(time.time()) - int(slack_ts_timeout):
        #         print('Slack timestamp is older than ' +
        #               str(slack_ts_timeout) + ' seconds. Posting a new message.')
        #         del item['slack_ts']
        # else:
        #     print('No slack_ts for existing digest. Skipping Slack post.')
        #     update_slack = False
        # Compare events and reconcile.
        print(("EXISTING DIGEST DETECTED: Id " + event_id + " - reconciling"))
        item["tasks"][task_id] = event_detail["lastStatus"]

        if "stoppedReason" in event_detail:
            if "stoppedReason" in item:
                item["stoppedReason"][task_id] = event_detail["stoppedReason"]
            else:
                item["stoppedReason"] = {task_id: event_detail["stoppedReason"]}

        assign_task_to_group(item, task_id)

    else:
        print(("CREATING NEW DIGEST: Id " + event_id))
        td = get_task_definition(event_detail["taskDefinitionArn"])
        containers = td["containerDefinitions"]
        images = []
        for c in containers:
            images.append(c["image"].split("/")[-1])

        item = {
            "startedBy": event_id,
            "cluster": event_detail["clusterArn"].split("/")[-1],
            "service": event_detail["group"].split(":")[-1],
            "definition": event_detail["taskDefinitionArn"].split("/")[-1],
            "tasks": {task_id: event_detail["lastStatus"]},
            "updatedAt": event_detail["updatedAt"],
            "createdAt": event_detail["createdAt"],
            "images": images,
            "task_groups": [[task_id]],
        }
    if included_clusters.lower() == "all":
        update_slack = True
    elif item["cluster"] not in included_clusters.split(","):
        update_slack = False
    if update_slack:
        post_update_to_slack(event, item)
    ttl_value = int(time.time()) + int(digest_item_ttl)
    item["TTL"] = ttl_value
    # Store the updated item in dynamodb
    table.put_item(Item=item)


def build_group_fields(item, task_ids, task_arn):
    """
    Compute the Slack attachment color/fields for a single group of task
    IDs within a digest (a subset of item["tasks"]/item["stoppedReason"]).
    """
    rs = ["RUNNING", "STOPPED"]
    group_tasks = {tid: item["tasks"][tid] for tid in task_ids if tid in item["tasks"]}

    stats = {}
    completed = Counter(x for x in group_tasks.values() if x in rs)
    stats["completed"] = "\n".join(["{}: {}".format(*x) for x in completed.items()])

    in_progress = Counter(x for x in group_tasks.values() if x not in rs)
    stats["in_progress"] = "\n".join(["{}: {}".format(*x) for x in in_progress.items()])

    group_stopped_reasons = {
        tid: reason
        for tid, reason in item.get("stoppedReason", {}).items()
        if tid in group_tasks
    }
    if group_stopped_reasons:
        failed = Counter(
            x
            for x in group_stopped_reasons.values()
            if not x.startswith("Scaling activity")
        )
        stats["failed"] = "\n".join(["{}: {}".format(*x) for x in failed.items()])

    fields = [
        {"title": "Completed", "value": stats["completed"], "short": "true"},
    ]
    if len(stats["in_progress"]) == 0:
        color = "good"
    else:
        color = "warning"
        fields.append(
            {"title": "In Progress", "value": stats["in_progress"], "short": "true"},
        )
    if stats.get("failed"):
        color = "danger"
        fields.append(
            {"title": "Failed", "value": stats["failed"], "short": "false"},
        )
        if task_arn in task_ids:
            fields.append(
                {"title": "TaskID", "value": task_arn, "short": "true"},
            )

    return color, fields


def post_update_to_slack(event, item):
    """
    Post or update the Slack message for the task group affected by this
    event - not every group in the digest.

    Deployments that accumulate more than slack_group_size tasks (e.g. a
    crash loop) are split across multiple messages instead of growing one
    message indefinitely - 110 tasks with the default group size of 50
    becomes 3 messages (50/50/10). Only the group containing the task this
    event is about is touched, so a single task update costs one Slack API
    call regardless of how many groups the deployment has. Mutates
    item["slack_ts"] (a dict of group index -> Slack message timestamp) in
    place.
    """
    e = event["detail"]
    cluster = e["clusterArn"].split("/")[-1]
    service = e["group"].split(":")[-1]
    td = e["taskDefinitionArn"].split("/")[-1]
    task_arn = e["taskArn"].split("/")[-1]
    ecs_url = "https://console.aws.amazon.com/ecs/home?region=" + region + "#/"
    srv_url = ecs_url + "clusters/" + cluster + "/services/" + service + "/tasks"
    td_url = ecs_url + "taskDefinitions/" + td.replace(":", "/")
    td_link = "<" + td_url + "|" + td + ">"

    channel_id = get_slack_channel_id(channel)
    if not channel_id:
        print(f"Error: Could not resolve channel '{channel}'")
        return

    groups = item.get("task_groups") or chunk_list(
        list(item["tasks"].keys()), slack_group_size
    )

    index = next((i for i, task_ids in enumerate(groups) if task_arn in task_ids), None)
    if index is None:
        print(f"Error: Task {task_arn} not found in any task_groups bucket - skipping Slack post")
        return

    # Digests created before grouping support store slack_ts as a bare
    # string for their single message - treat that as group "0".
    slack_ts = item.get("slack_ts") or {}
    if isinstance(slack_ts, str):
        slack_ts = {"0": slack_ts}

    title = "{} {} - {}".format(cluster, service, " ".join(item["images"]))

    task_ids = groups[index]
    color, fields = build_group_fields(item, task_ids, task_arn)
    group_title = title
    if len(groups) > 1:
        group_title += " (part {} of {})".format(index + 1, len(groups))

    attachments = [
        {
            "title": group_title,
            "title_link": srv_url,
            "color": color,
            "fields": fields,
            "footer": "[ecs {}] {} {}".format(
                e["launchType"].lower(), td_link, e["startedBy"]
            ),
        }
    ]

    key = str(index)
    res = None
    try:
        if key in slack_ts:
            res = wc.chat_update(
                ts=slack_ts[key],
                channel=channel_id,
                attachments=attachments,
            )
            print(f"Slack message for group {index} updated successfully")
        else:
            res = wc.chat_postMessage(
                channel=channel_id,
                attachments=attachments,
            )
            print(f"Slack message for group {index} posted successfully")

        ts = res.get("ts") or res.get("message", {}).get("ts")
        if ts:
            slack_ts[key] = ts

        print("Slack response:")
        print(res)

    except SlackApiError as se:
        print(f"Error posting to Slack for group {index}: {se.response['error']}")
        print(f"Full error response: {se.response}")
    except KeyError as ke:
        print(f"Error: Cannot get slack timestamp from response. Key error: {ke}")
        print(f"Response was: {res}")

    item["slack_ts"] = slack_ts


def get_task_definition(td):
    try:
        ecs = boto3.client("ecs", region_name=region)
        res = ecs.describe_task_definition(taskDefinition=td)
    except ClientError as e:
        print((e.response["Error"]["Message"]))
        raise
    return res["taskDefinition"]


def get_dynamo_item(table, key, value):
    item = table.get_item(Key={key: value})
    return item
