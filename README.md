# user\_pinger

This is a Reddit bot that sends messages to members of a group in response to pings. The main instance of this bot is running on the subreddit /r/neoliberal using the account /u/groupbot. For more information on how to use the bot, please [see this documentation page from the /r/neoliberal wiki](https://reddit.com/r/neoliberal/wiki/userpinger/documentation).

To deploy this bot, install the requirements, ensure you have the required envrionment variables set (see `service.py`) and then run `python service.py`.

For more information, please [contact the moderators of /r/neoliberal](https://www.reddit.com/message/compose?to=%2Fr%2Fneoliberal) (requires a Reddit account) or open an issue on this page.

## Adding Groups

There are two ways of creating a group.

The easy method is for a moderator to send /u/groupbot a message with the body "creategroup [GROUP]". This will create the new group and add the moderator who sent the message as its only member. Then users can join the group as normal.

The more difficult method is to go directly to the [userpinger/config/groups page](https://www.reddit.com/r/neoliberal/wiki/userpinger/config/groups). On this page you can click "edit" and add the group and its members. Just be sure tto follow the formatting *exactly* or you could crash groupbot.

Lastly, make sure you add the group to our [documentation page](https://www.reddit.com/r/neoliberal/wiki/userpinger/documentation). Otherwise people might not know it exists.
