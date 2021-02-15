import os
import pprint
import argparse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import ldap
import requests

def ldap_get_group_users(url, base, attribute, group):
  """Retrieve the membership list of `group` from the specified `attribute`"""

  con = ldap.initialize(url)
  con.set_option(ldap.OPT_REFERRALS, 0)
  con.simple_bind_s('', '')
  result = con.search_s(base, ldap.SCOPE_SUBTREE, 'cn=' + group, [attribute])

  return [str(s, 'utf-8') for s in result[0][1][attribute]]

class Slack(object):
  """Abstract away some of the interactions with Slack"""

    def __init__(self, token):
      self.token = token
      self.client = None
      self._user_id_cache = {}
      self._display_name_cache = {}

    def _get_client(self):
      if (not self.client):
        self.client = WebClient(token=self.token)
      
      return self.client

    def _update_cache(self, user):
      """We keep a cache from `display_name` -> `user` and Slack `id` -> `user`"""

      self._display_name_cache[user['profile']['display_name']] = user
      self._user_id_cache[user['id']] = user

    def get_user_by_display_name(self, display_name):
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

      # otherwise known as the "guinea pig"
      first_user = self._user_id_cache[list(self._user_id_cache.keys())[0]]

      enterprise_id = first_user['enterprise_user']['enterprise_id']
      team_id = first_user['enterprise_user']['teams'][0]

      result = requests.post(
        f'https://edgeapi.slack.com/cache/{enterprise_id}/{team_id}/users/search', 
        f'{{"token":"{self.token}","query":"{display_name}","count":1,"fuzz":1,"uax29_tokenizer":false,"filter":"NOT deactivated"}}')

      user = result.json()['results'][0]

      self._update_cache(user)

      return user

    def get_user_by_id(self, user_id, humans_only = True):

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
      client = self._get_client()

      try:
          response = client.conversations_members(channel=args.channel)

          users = []

          for user_id in response["members"]:
            response = client.users_info(user=user_id)

            user = self.get_user_by_id(user_id)

            if (user):
              users.append(user['name'])

            #pprint.pprint(user)

          return users

      except SlackApiError as e:
          print(e)

    def add_users_to_channel(self, channel, users):
      client = self._get_client()

      user_ids = [self.get_user_by_display_name(user)['id'] for user in users]

      try:
          response = client.conversations_invite(channel=channel, users=user_ids)

      except SlackApiError as e:
        print(e)


    def remove_user_from_channel(self, channel, display_name):
      user = self.get_user_by_display_name(display_name)

      client = self._get_client()

      try:
        response = client.conversations_kick(channel=channel, user=user['id'])

      except SlackApiError as e:
        print(e)

    def remove_users_from_channel(self, channel, display_names):
      [self.remove_user_from_channel(channel, display_name) for display_name in display_names]

parser = argparse.ArgumentParser(description='Sync a Slack channel\'s membership to an LDAP group')
parser.add_argument('-t', '--token', required=True, help='Slack access token')
parser.add_argument('-c', '--channel', required=True, help='Channel ID (not name!)')
parser.add_argument('-u', '--ldap-url', required=True, help='URL of your LDAP endpoint. usually LDAP://...')
parser.add_argument('-b', '--ldap-base', required=True, help='Search base for finding the LDAP group. For example, ou=groups,o=awesome.co')

# memberuid is technically deprecated, but seems to still be around and is often the cleanest, most useful
# list of user ids
parser.add_argument('-a', '--ldap-attribute', default="memberuid", help='attribute to retrieve during LDAP queries. This should be the list of users within a group that will be used to sync with Slack')

parser.add_argument('-g', '--group', required=True, help='LDAP group to query for')
parser.add_argument('-m', '--welcome-message', help="Welcome message for new members. New members will be @mentioned.")
parser.add_argument('--remove', action='store_true', help='remove members not in group (default is to just add)')
parser.add_argument('--dryrun', action='store_true', help='don\'t actually make any changes')
parser.add_argument('--debug', action='store_true', help='largely unhelpful spew')

args = parser.parse_args()

users_in_group = ldap_get_group_users(args.ldap_url, args.ldap_base, args.ldap_attribute, args.group)

slack = Slack(args.token)
users_in_channel = slack.get_channel_users(args.channel)

users_to_add = [user for user in users_in_group if user not in users_in_channel]
users_to_remove = [user for user in users_in_channel if user not in users_in_group]

slack.add_users_to_channel(args.channel, users_to_add)

if (args.remove):
  slack.remove_users_from_channel(args.channel, users_to_remove)
elif (len(users_to_remove)):
  print('The following users would have been removed (to remove them rerun with --remove set):')
  print(*users_to_remove, sep=', ')
  print()
else:
  print('No users to remove, even if the --remove flag had been set')
