import pprint
import logging
import argparse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import ldap
import requests

def ldap_get_group_users(url, base, attribute, group):
  """Retrieve the membership list of `group` from the specified `attribute`"""

  print(f'Getting users from LDAP group "{group}"...')

  con = ldap.initialize(url)
  con.set_option(ldap.OPT_REFERRALS, 0)
  con.simple_bind_s('', '')
  result = con.search_s(base, ldap.SCOPE_SUBTREE, 'cn=' + group, [attribute])

  users = result[0][1][attribute]

  logger.debug(f'Found {len(users)}')

  return [str(s, 'utf-8') for s in users]

class Slack(object):
  """Abstract away some of the interactions with Slack"""

  def __init__(self, token):
    self.token = token
    self.client = None
    self._user_id_cache = {}
    self._display_name_cache = {}
    self._enterprise_id = None
    self._team_id = None

  def _get_client(self):
    if (not self.client):
      self.client = WebClient(token=self.token)
    
    return self.client

  def _update_cache(self, user):
    """We keep a cache from `display_name` -> `user` and Slack `id` -> `user`"""

    self._display_name_cache[user['profile']['display_name']] = user
    self._user_id_cache[user['id']] = user

  def _get_enterprise_details(self):

    if (self._enterprise_id and self._team_id):
      return (self._enterprise_id, self._team_id)

    logger.debug('Getting enterprise details...')

    client = self._get_client()

    try:
      response = client.users_list(limit=1)

      user = response['members'][0]
      enterprise_user = user['enterprise_user']

      self._enterprise_id = enterprise_user['enterprise_id']
      self._team_id = user['team_id']

      return (self._enterprise_id, self._team_id)

    except SlackApiError as e:
        print(e)

  def get_user_by_display_name(self, display_name):
    logger.debug(f'Looking up user by display name: {display_name}')

    if (display_name in self._display_name_cache):
      return self._display_name_cache[display_name]

    """
    This is cheating... horribly. Slack can either retrieve a user if you already know
    their ID (useless when syncing a user name from LDAP) or via email (useful, but
    very often not a permission that is granted, so calls to users.lookupByEmail fail).

    To work around this we are using an undocumented API `users/search` but for that we need
    to know the enterprise ID and a team ID. For that we take the first cached user (presumably
    one exists since to have a channel there will be at least one member), extract the IDs and
    then hit the API.

    Note: This likely won't work for non-enterprise users, but those are the ones that are
          most likely to have an LDAP server they want to sync to...
    """

    (enterprise_id, team_id) = self._get_enterprise_details()

    result = requests.post(
      f'https://edgeapi.slack.com/cache/{enterprise_id}/{team_id}/users/search',
      f'{{"token":"{self.token}","query":"{display_name}","count":25,"fuzz":1,"uax29_tokenizer":false,"filter":"NOT deactivated"}}')

    for user in result.json()['results']:
      if user['profile']['display_name'] == display_name:
        self._update_cache(user)
        return user

    return None

  def get_channel_by_name(self, name):
    logger.debug(f'Looking up channel by name: {name}')

    """
    This is also cheating... see note in `get_user_by_display_name`
    """

    (enterprise_id, team_id) = self._get_enterprise_details()

    result = requests.post(
      f'https://edgeapi.slack.com/cache/{enterprise_id}/{team_id}/channels/search',
      f'{{"token":"{self.token}","query":"{name}","count":25,"fuzz":1,"uax29_tokenizer":false}}')

    for channel in result.json()['results']:
      if channel['name'] == name:
        return channel['id']

    return None

  def get_user_by_id(self, user_id, humans_only = True):
    logger.debug(f'Looking up user by ID: {user_id} (humans only? {humans_only})')

    if (user_id in self._user_id_cache):
      return self._user_id_cache[user_id]

    client = self._get_client()

    try:
      response = client.users_info(user=user_id)

      user = response["user"]
      self._update_cache(user)
      
      if (not humans_only or (not user['is_bot'] and ('is_workflow_bot' not in user or not user['is_workflow_bot']))):
        return user
      
      return None

    except SlackApiError as e:
        print(e)

  def get_channel_users(self, channel, humans_only = True):

    print(f'Getting users for channel {channel}')

    client = self._get_client()

    try:
        response = client.conversations_members(channel=channel)

        users = []

        logger.debug(f"Found {len(response['members'])} in channel {channel}.")

        for user_id in response['members']:
          response = client.users_info(user=user_id)

          user = self.get_user_by_id(user_id)

          if (user):
            users.append(user['name'])

        print(f' -> found {len(users)} users')

        return users

    except SlackApiError as e:
        print(e)

  def add_users_to_channel(self, channel, users):
    print(f'Adding {len(users)} users to {channel}...')

    client = self._get_client()

    user_ids = [self.get_user_by_display_name(user)['id'] for user in users]

    try:
        response = client.conversations_invite(channel=channel, users=user_ids)

    except SlackApiError as e:
      print(e)


  def remove_user_from_channel(self, channel, display_name):
    print(f'Kicking {display_name} from {channel}...')

    user = self.get_user_by_display_name(display_name)

    client = self._get_client()

    try:
      response = client.conversations_kick(channel=channel, user=user['id'])

    except SlackApiError as e:
      print(e)

  def remove_users_from_channel(self, channel, display_names):
    [self.remove_user_from_channel(channel, display_name) for display_name in display_names]

def _init_logging(debug = False):
  logger = logging.getLogger(__name__)

  logger.setLevel(logging.DEBUG if debug else logging.INFO)

  # create console handler and set level to debug
  ch = logging.StreamHandler()
  ch.setLevel(logging.DEBUG)

  # create formatter
  formatter = logging.Formatter('[%(asctime)s | %(levelname)s: %(funcName)20s()] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

  # add formatter to ch
  ch.setFormatter(formatter)

  # add ch to logger
  logger.addHandler(ch)

  return logger

parser = argparse.ArgumentParser(description='Sync a Slack channel\'s membership to an LDAP group')
parser.add_argument('-t', '--token', required=True, help='Slack access token')

channel_arg_group = parser.add_mutually_exclusive_group(required=True)
channel_arg_group.add_argument('-i', '--channel-id', help='Channel ID (not name!)')
channel_arg_group.add_argument('-c', '--channel', help='Channel name')

parser.add_argument('-u', '--ldap-url', required=True, help='URL of your LDAP endpoint. usually LDAP://...')
parser.add_argument('-b', '--ldap-base', required=True, help='Search base for finding the LDAP group. For example, ou=groups,o=awesome.co')

# memberuid is technically deprecated, but seems to still be around and is often the cleanest, most useful
# list of user ids
parser.add_argument('-a', '--ldap-attribute', default="memberuid", help='attribute to retrieve during LDAP queries. This should be the list of users within a group that will be used to sync with Slack')

parser.add_argument('-g', '--group', required=True, help='LDAP group to query for')
parser.add_argument('-m', '--welcome-message', help="Welcome message for new members. New members will be @mentioned.")
parser.add_argument('--remove', action='store_true', help='remove members not in group (default is to just add)')
parser.add_argument('--dryrun', action='store_true', help='don\'t actually make any changes')
parser.add_argument('-d', '--debug', action='store_true', help='largely unhelpful spew')

args = parser.parse_args()

logger = _init_logging(debug=args.debug)

users_in_group = ldap_get_group_users(args.ldap_url, args.ldap_base, args.ldap_attribute, args.group)

slack = Slack(args.token)
channel_id = args.channel_id or slack.get_channel_by_name(args.channel)

users_in_channel = slack.get_channel_users(channel_id)

users_to_add = [user for user in users_in_group if user not in users_in_channel]
users_to_remove = [user for user in users_in_channel if user not in users_in_group]

if (len(users_to_add) > 0):
  slack.add_users_to_channel(channel_id, users_to_add)
else:
  print("No users to add.")

if (args.remove):
  slack.remove_users_from_channel(channel_id, users_to_remove)
elif (len(users_to_remove)):
  print('The following users would have been removed (to remove them rerun with --remove set):')
  print(*users_to_remove, sep=', ')
  print()
else:
  print('No users to remove, even if the --remove flag had been set.')
