import os
import boto3
import json

from botocore.exceptions import ClientError, NoRegionError

from urlparse import parse_qs
from slackclient import SlackClient

region = os.environ['AWS_DEFAULT_REGION']
slack_token = os.environ["SLACK_API_TOKEN"]
expected_token = os.environ['SLACK_VERIFICATION_TOKEN']
notifications_channel = os.environ['SLACK_CHANNEL']
notifications_filter = os.environ['INCLUDED_CLUSTERS']


def get_slack_channel_id(channel_name):
    sc = SlackClient(slack_token)

    channels = sc.api_call(
        'channels.list',
        exclude_archived='true',
        exclude_members='true',
    )
    for c in channels['channels']:
        if c['name'] == channel_name:
            channel_id = c['id']

    return channel_id


def help():
    msg = {'text': 'Usage: /ecs-deploy [cluster] [service] [reference]'}
    return create_msg_payload(attachments=msg, response_type='ephemeral')


def verify_slack_token(token):
    if token != expected_token:
        print('Request token ' + token + ' does not match expected')
        raise Exception('Invalid request token')


def register_task_def_with_new_image(ecs, ecr, cluster, service, artifact):
    # Get ECR repo
    srv = desc_service(ecs, cluster, service)
    td_arn = srv['taskDefinition']
    print('Current task deinition for {} {}: {}'.format(
        cluster,
        service,
        td_arn.split('/')[-1]
    ))
    td = desc_task_definition(ecs, td_arn)
    containers = td['containerDefinitions']

    try:
        ecr_repo, ecr_image_tag = containers[0]['image'].split(':')
    except ValueError:
            # If no tag was specified - defaulting to latest tag
        ecr_repo = containers[0]['image']

    # Check if image tag exist in the ECR repo
    try:
        ecr.describe_images(
            repositoryName=ecr_repo.split('/')[-1],
            imageIds=[
                {
                    'imageTag': artifact
                },
            ],
            filter={
                'tagStatus': 'TAGGED'
            }
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ImageNotFoundException':
            raise ValueError('Image not found: {}'.format(e))
        else:
            raise RuntimeError(e)

    print('Found image: {}:{}'.format(ecr_repo, artifact))

    ###########################################################################
    # Force new deployment with the current active task definition if
    # the requested for docker image tag is the same.
    # We need to recycle containers in case the tag was reassigned to different
    # docker image (think tag:latest).
    # We skip registering a new task definition revision as it's not needed.
    ###########################################################################
    if ecr_image_tag and ecr_image_tag == artifact:
        print('{}:{} is already in the current task definition.'.format(
            ecr_repo.split('/')[-1],
            ecr_image_tag
        ))
        print('Forcing a new deployment of {}'.format(td_arn.split('/')[-1]))
        return td_arn

    ###########################################################################
    # Register new task definition with the new image
    ###########################################################################
    new_td = td.copy()
    for k in ['status', 'compatibilities', 'taskDefinitionArn',
              'revision', 'requiresAttributes']:
        del new_td[k]
    new_td['containerDefinitions'][0]['image'] = ':'.join([ecr_repo, artifact])
    new_td_res = ecs.register_task_definition(**new_td)
    td_name = new_td_res['taskDefinition']['taskDefinitionArn'].split('/')[-1]
    print('Registered new task definition: {}'.format(td_name))

    return td_name


def deploy_task_definition(ecs, cluster, service, task_def):
    print('Deploying {} to {} {}...'.format(
        task_def.split('/')[-1],
        cluster,
        service
    ))
    params = {
        'cluster': cluster,
        'service': service,
        'taskDefinition': task_def,
        'forceNewDeployment': True
    }

    res = ecs.update_service(**params)
    return res


def desc_service(ecs, cluster, service):
    try:
        res = ecs.describe_services(
            cluster=cluster,
            services=[service]
        )
        srv = res['services'][0]
    except ClientError as e:
        if e.response['Error']['Code'] == 'ClusterNotFoundException':
            raise ValueError('Cluster not found.')
        else:
            raise RuntimeError(e)
    except IndexError:
        raise ValueError('Service not found.')

    return srv


def desc_task_definition(ecs, taskDefinition):
    res = ecs.describe_task_definition(taskDefinition=taskDefinition)
    return res['taskDefinition']


def create_msg_payload(channel=None, username=None, attachments=None,
                       text=None, response_type='in_channel',
                       replace_original='false', delete_original='false'):
    if not isinstance(attachments, list):
        attachments = [attachments]
    payload = {
        'response_type': response_type,
        'channel': channel,
        'username': username,
        'text': text,
        'attachments': attachments,
        'replace_original': replace_original,
        'delete_original': delete_original
    }
    return payload


def handle_slack_command(params):
    token = params['token'][0]
    try:
        verify_slack_token(token)
    except Exception as e:
        handle_error(e)

    # Parse the slack command
    try:
        command_text = params['text'][0]
        cluster, service, reference = command_text.split()
    except ValueError:
        return help()

    sess = boto3.session.Session(region_name=region)
    try:
        ecs = sess.client('ecs')
        ecr = sess.client('ecr')
    except NoRegionError as e:
        return handle_error(e)

    try:
        td = register_task_def_with_new_image(
            ecs, ecr, cluster, service, reference)
        res = deploy_task_definition(ecs, cluster, service, td)
    except Exception as e:
        return handle_error(e)

    td = res['service']['deployments'][0]['taskDefinition'].split('/')[-1]
    msg = 'Deploying {}.'.format(td)
    if cluster in notifications_filter:
        cid = get_slack_channel_id(notifications_channel)
        msg += ' Notifications in <#{}|{}>'.format(cid, notifications_channel)
    payload = create_msg_payload(text=msg)

    return payload


def response(statusCode, body):
    print('LAMBDA RESPONSE:')
    print(body)
    return {
        'statusCode': statusCode,
        'body': json.dumps(body, ensure_ascii=False)
    }


def handle_error(error_message):
    payload = create_msg_payload(text=str(error_message))
    return payload


def handler(event, context):
    req_body = event['body']
    params = parse_qs(req_body)
    print(params)

    msg = handle_slack_command(params)
    return response(200, msg)
