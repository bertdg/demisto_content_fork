import json
import time

import requests

import demistomock as demisto
import urllib3
from CommonServerPython import *  # noqa # pylint: disable=unused-wildcard-import
from CommonServerUserPython import *  # noqa
import traceback
from typing import Dict

# Disable insecure warnings
urllib3.disable_warnings()  # pylint: disable=no-member


''' CONSTANTS '''

EVENTS_OBJECT_KEYS = {'events': 'id'}

''' CLIENT CLASS '''


class Client(BaseClient):
    """Client class to interact with the service API

    This Client implements API calls to the Saas Security platform, and does not contain any XSOAR logic.
    Handles the token retrieval.

    :param base_url (str): Saas Security server url.
    :param client_id (str): client ID.
    :param client_secret (str): client secret.
    :param verify (bool): specifies whether to verify the SSL certificate or not.
    :param proxy (bool): specifies if to use XSOAR proxy settings.
    """

    def __init__(self, base_url: str, client_id: str, client_secret: str, verify: bool, proxy: bool, **kwargs):
        self.client_id = client_id
        self.client_secret = client_secret

        super().__init__(base_url=base_url, verify=verify, proxy=proxy, **kwargs)

    def http_request(self, *args, **kwargs):
        """
        Overrides Base client request function, retrieves and adds to headers access token before sending the request.

        :return: The http response
        """
        if kwargs.pop('is_test'):
            token = self.get_access_token().get('access_token')
        else:
            token = json.loads(demisto.getIntegrationContext().get('access_token'))
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }
        return super()._http_request(*args, headers=headers, **kwargs)

    def get_access_token(self):
        """
       Obtains access and refresh token from server.
       Access token is used and stored in the integration context until expiration time.
       After expiration, new refresh token and access token are obtained and stored in the
       integration context.

       :return: Access token that will be added to authorization header.
       :rtype: dict
       """
        return {'access_token': self.get_token_request()}

    def get_token_request(self):
        """
        Sends request to retrieve token.

       :return: Access token.
       :rtype: str
        """
        base64_encoded_creds = b64_encode(f'{self.client_id}:{self.client_secret}')
        headers = {
            'accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded; charset=ISO-8859-1',
            'Authorization': f'Basic {base64_encoded_creds}',
        }
        params = {
            'grant_type': 'client_credentials',
            'scope': 'api_access',
        }
        token_response = self._http_request('POST', url_suffix='/oauth/token', params=params, headers=headers)
        return token_response.get('access_token')

    def get_events_request(self, retries=5, is_test=False):
        return self.http_request(
            'GET',
            url_suffix='/api/v1/log_events',
            resp_type='response',
            status_list_to_retry=[204],
            retries=retries,
            ok_codes=[200, 401, 204],
            is_test=is_test
        )


''' COMMAND FUNCTIONS '''


def test_module(client: Client):
    # could cause us to lose events, need to check how to handle if possible
    response = client.get_events_request(retries=1, is_test=True)  # retries 1 to avoid waiting so long for test module
    if response.status_code in (200, 204):
        return 'ok'


def get_events_from_integration_context(is_fetch_events: bool = False, max_fetch: int = 10) -> List[dict]:
    """
    Get the events that were ingested into the integration context by the long running integration.

    When fetching, to avoid from duplicates we want to remove the cached events.
    When just getting events, we can return them without deleting them from context integration, to avoid losing events.

    Args:
        is_fetch_events (bool): whether fetch-events was called.
        max_fetch (int): how many events to fetch each time.

    Returns:
        List[dict]: The events from the integration context.
    """
    integration_context = demisto.getIntegrationContext()
    demisto.log(f'get events context: {integration_context}')
    context_events = json.loads(integration_context.get('events', '[]'))

    fetched_events = context_events[:max_fetch]

    if is_fetch_events:
        # if we are fetching events, in order to avoid duplicates we must remove events that we are fetching now.
        for i in range(min(max_fetch, len(context_events))):
            context_events[i]['remove'] = True

        # remove only the the events that were fetched.
        set_to_integration_context_with_retries(
            context={'events': context_events[:max_fetch]},
            object_keys=EVENTS_OBJECT_KEYS
        )

    demisto.debug(f'integration context events len: ({len(context_events)}), content: ({context_events})')
    return fetched_events


def saas_security_get_events_command(args: Dict) -> Union[str, CommandResults]:
    """
    Executes the saas-security-get-events commands.
    Returns the events that are stored in the context output.
    """
    limit = arg_to_number(args.get('limit')) or 10
    if events := get_events_from_integration_context(max_fetch=limit):
        return CommandResults(
            readable_output=tableToMarkdown(
                'SaaS Security Logs', events, headers=list(events[0].keys()), headerTransform=underscoreToCamelCase
            ),
            raw_response=events,
            outputs=events,
            outputs_key_field='id',
            outputs_prefix='SaasSecurity.Event'
        )
    return 'No events were found.'


def fetch_events(max_fetch: int) -> List[Dict]:
    """
    Fetches events that are stored in the integration context.
    """
    return get_events_from_integration_context(is_fetch_events=True, max_fetch=max_fetch)


def long_running_execution_command(client: Client):
    """
    Stores fetched events in the integration context and in addition make sure the access token is always up to date
    at all points of time.
    """
    # set the access token for the first fetch
    set_to_integration_context_with_retries(context=client.get_access_token())
    # there is no unique id for events, hence need to make one of our own.
    current_event_id = 1

    while True:
        try:
            updated_context = {}
            demisto.debug(f'context integration: {demisto.getIntegrationContext()}')

            response = client.get_events_request()
            if response.status_code == 200:
                events = response.json()
                demisto.debug(f'Received event(s): {events}')
                if isinstance(events, list):  # 'events' is a list of events
                    for event in events:
                        event['id'] = current_event_id
                    updated_context['events'] = events
                else:  # 'events' is a single dict (only one event)
                    events['id'] = current_event_id
                    updated_context['events'] = [events]
                current_event_id += 1
            elif response.status_code == 401:
                demisto.debug(f'Unauthorized: [{response.json()}]')
                # update the access token in case its required
                updated_context.update(client.get_access_token())
            elif response.status_code == 204:
                demisto.debug(f'204 - No Content when fetching events')

            if updated_context:
                # update integration context only in cases where there is anything to update, otherwise avoid
                # setting the integration context as much as possible
                set_to_integration_context_with_retries(
                    context=updated_context,
                    object_keys=EVENTS_OBJECT_KEYS
                )
        except Exception as e:
            demisto.error(traceback.format_exc())
            demisto.error(f'Error occurred in long running execution: [{e}]')


def main() -> None:
    params = demisto.params()
    client_id: str = params.get('credentials', {}).get('identifier', '')
    client_secret: str = params.get('credentials', {}).get('password', '')
    base_url: str = params.get('url', '').rstrip('/')
    verify_certificate = not params.get('insecure', False)
    proxy = params.get('proxy', False)
    max_fetch = arg_to_number(params.get('max_fetch')) or 10
    args = demisto.args()

    command = demisto.command()
    demisto.info(f'Command being called is {command}')
    try:
        client = Client(
            base_url=base_url,
            client_id=client_id,
            client_secret=client_secret,
            verify=verify_certificate,
            proxy=proxy,
        )

        if command == "test-module":
            results = test_module(client)
            return_outputs(results)
        elif command == 'long-running-execution':
            long_running_execution_command(client)
        elif command == 'fetch-events':
            send_events_to_xsiam(events=fetch_events(max_fetch=max_fetch), vendor='Palo Alto', product='saas-security')
        elif command == 'saas-security-get-events':
            return_results(saas_security_get_events_command(args=args))
        else:
            raise ValueError(f'Command {command} is not implemented in saas-security integration.')
    except Exception as e:
        demisto.error(traceback.format_exc())
        raise Exception(f'Error in Palo Alto Saas Security Event Collector Integration [{e}]')


''' ENTRY POINT '''


if __name__ in ('__main__', '__builtin__', 'builtins'):
    main()
