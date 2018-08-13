#!/usr/bin/env python3
# encoding: utf-8

import asyncio
import contextlib
import io
import logging
import re
import traceback
import weakref

logger = logging.getLogger(__name__)

import aiohttp
import asyncpg
import discord
from discord.ext import commands

from .db import DatabaseEmote
from .. import utils
from ..utils import image as image_utils
from ..utils import checks
from ..utils import errors
from ..utils.paginator import Pages


class Emotes:
	"""Commands related to the main functionality of the bot"""

	def __init__(self, bot):
		self.bot = bot
		self.db = self.bot.get_cog('Database')
		self.logger = self.bot.get_cog('Logger')
		self.http = aiohttp.ClientSession(loop=self.bot.loop, read_timeout=30, headers={
			'User-Agent':
				self.bot.config['user_agent'] + ' '
				+ self.bot.http.user_agent
		})

		# Keep track of replies so that if the user edits/deletes a message,
		# we delete/edit the corresponding reply.
		# Each message supposedly takes up about 256 bytes of RAM.
		# Don't store more than 1MiB of them.
		self.replies = utils.LRUDict(size=1024**2//256)

		# keep track of created paginators so that we can remove their reaction buttons on unload
		self.paginators = weakref.WeakSet()

	def __unload(self):
		# aiohttp can't decide if this should be a coroutine...
		# i think it shouldn't be, since it never awaits
		self.bot.loop.create_task(self.http.close())
		for paginator in self.paginators:
			self.bot.loop.create_task(paginator.stop(delete=False))

	## Commands

	@commands.command()
	@checks.not_blacklisted()
	async def info(self, context, emote: DatabaseEmote):
		"""Gives info on an emote.

		The emote must be in the database.
		"""
		embed = discord.Embed()

		if emote.preserve:
			embed.title = _('{emote} (Preserved)').format(emote=emote.with_name())
		else:
			embed.title = emote.with_name()

		if emote.description is not None:
			embed.description = emote.description

		if emote.created is not None:
			embed.timestamp = emote.created
			embed.set_footer(text=_('Created'))

		avatar = None
		with contextlib.suppress(AttributeError):
			avatar = self.bot.get_user(emote.author).avatar_url_as(static_format='png', size=32)

		name = utils.format_user(self.bot, emote.author, mention=False)
		if avatar is None:
			embed.set_author(name=name)
		else:
			embed.set_author(name=name, icon_url=avatar)

		if emote.modified is not None:
			embed.add_field(
				name=_('Last modified'),
				# hangul filler prevents the embed fields from jamming next to each other
				value=utils.format_time(emote.modified) + '\N{hangul filler}')

		embed.add_field(name='Usage count', value=await self.db.get_emote_usage(emote))

		await context.send(embed=embed)

	@commands.command()
	async def gimme(self, context, emote: DatabaseEmote):
		"""Lets you join the server that has the emote you specify.

		If you have nitro, this will let you use it anywhere!
		"""

		guild = self.bot.get_guild(emote.guild)
		invite = await guild.text_channels[0].create_invite(
			max_age=600,
			max_uses=2,
			reason=_('Created for {user}').format(
				user=utils.format_user(self.bot, context.author, mention=False)))

		try:
			await context.author.send(_(
				'Invite to the server that has {emote}: {invite.url}').format(**locals()))
		except discord.Forbidden:
			await context.send(_('Unable to send invite in DMs. Please allow DMs from server members.'))
		else:
			with contextlib.suppress(discord.HTTPException):
				await context.message.add_reaction('📬')

	@commands.command()
	@checks.not_blacklisted()
	async def count(self, context):
		"""Tells you how many emotes are in my database."""
		static, animated, total = await self.db.count()
		static_cap, animated_cap, total_cap = self.db.capacity()
		await context.send(_(
			'Static emotes: **{static} ⁄ {static_cap}**\n'
			'Animated emotes: **{animated} ⁄ {animated_cap}**\n'
			'**Total: {total} ⁄ {total_cap}**').format(**locals()))

	@commands.command()
	@checks.not_blacklisted()
	async def big(self, context, emote: DatabaseEmote):
		"""Shows the original image for the given emote."""
		await context.send(f'{emote.name}: {emote.url}')

	@commands.command(rest_is_raw=True)
	@checks.not_blacklisted()
	async def quote(self, context, *, message):
		"""Quotes your message, with :foo: and ;foo; replaced with their emote forms"""
		message, _ = await self.quote_emotes(context.message, message)

		if context.guild.me.permissions_in(context.channel).manage_messages:
			# no space because rest_is_raw preserves the space after "ec/quote"
			message = _('{context.author.mention} said:').format(context) + message

			# it doesn't matter if they deleted their message before we sent our reply
			with contextlib.suppress(discord.NotFound):
				await context.message.delete()

		await context.send(message)

	@commands.command(aliases=['create'])
	@checks.not_blacklisted()
	async def add(self, context, *args):
		"""Add a new emote to the bot.

		You can use it like this:
		`ec/add :thonkang:` (if you already have that emote)
		`ec/add rollsafe https://image.noelshack.com/fichiers/2017/06/1486495269-rollsafe.png`
		`ec/add speedtest <https://cdn.discordapp.com/emojis/379127000398430219.png>`

		With a file attachment:
		`ec/add name` will upload a new emote using the first attachment as the image and call it `name`
		`ec/add` will upload a new emote using the first attachment as the image,
		and its filename as the name
		"""
		if context.message.webhook_id or context.author.bot:
			return await context.send(_(
				'Sorry, webhooks and bots may not add emotes. '
				'Go find a human to do it for you.'))

		try:
			name, url = self.parse_add_command_args(context, args)
		except commands.BadArgument as exception:
			return await context.send(exception)

		async with context.typing():
			message = await self.add_safe(name.strip(':;'), url, context.message.author.id)
		await context.send(message)

	def parse_add_command_args(self, context, args):
		if context.message.attachments:
			return self.parse_add_command_attachment(context, args)

		elif len(args) == 1:
			match = utils.emote.RE_CUSTOM_EMOTE.match(args[0])
			if match is None:
				raise commands.BadArgument(_(
					'Error: I expected a custom emote as the first argument, '
					'but I got something else. '
					"If you're trying to add an emote using an image URL, "
					'you need to provide a name as the first argument, like this:\n'
					'`{}add NAME_HERE URL_HERE`').format(context.prefix))
			else:
				animated, name, id = match.groups()
				url = utils.emote.url(id, animated=animated)

			return name, url

		elif len(args) >= 2:
			name = args[0]
			match = utils.emote.RE_CUSTOM_EMOTE.match(args[1])
			if match is None:
				url = utils.strip_angle_brackets(args[1])
			else:
				url = utils.emote.url(match.group('id'))

			return name, url

		elif not args:
			raise commands.BadArgument(_('Your message had no emotes and no name!'))

	@staticmethod
	def parse_add_command_attachment(context, args):
		attachment = context.message.attachments[0]
		# as far as i can tell, this is how discord replaces filenames when you upload an emote image
		name = ''.join(args) if args else attachment.filename.split('.')[0].replace(' ', '')
		url = attachment.url

		return name, url

	async def add_safe(self, name, url, author_id):
		"""Try to add an emote. Returns a string that should be sent to the user."""
		try:
			emote = await self.add_from_url(name, url, author_id)
		except errors.ConnoisseurError as ex:
			# yes, on_command_error will handle these, but other commands depend on this
			# function returning a string on error (such as steal_these)
			return str(ex)
		except discord.HTTPException as ex:
			return (
				_('An error occurred while creating the emote:\n')
				+ utils.format_http_exception(ex))
		except asyncio.TimeoutError:
			return _('Error: retrieving the image took too long.')
		except ValueError:
			return _('Error: Invalid URL.')
		else:
			return _('Emote {emote} successfully created.').format(emote=emote)

	async def add_from_url(self, name, url, author_id):
		# db.create_emote already does this, but do it again here so that we can fail early
		# in case resizing takes a long time.
		await self.db.ensure_emote_does_not_exist(name)

		image_data = await self.fetch_emote(url)
		emote = await self.create_emote_from_bytes(name, author_id, image_data)

		return emote

	async def fetch_emote(self, url):
		# credits to @Liara#0001 (ID 136900814408122368) for most of this part
		# https://gitlab.com/Pandentia/element-zero/blob/47bc8eeeecc7d353ec66e1ef5235adab98ca9635/element_zero/cogs/emoji.py#L217-228
		async with self.http.head(url, timeout=5) as response:
			if response.reason != 'OK':
				raise errors.HTTPException(response.status)
			if response.headers.get('Content-Type') not in ('image/png', 'image/jpeg', 'image/gif'):
				raise errors.InvalidImageError

		async with self.http.get(url) as response:
			if response.reason != 'OK':
				raise errors.HTTPException(response.status)
			return io.BytesIO(await response.read())

	async def create_emote_from_bytes(self, name, author_id, image_data: io.BytesIO):
		# resize_until_small is normally blocking, because wand is.
		# run_in_executor is magic that makes it non blocking somehow.
		# also, None as the executor arg means "use the loop's default executor"
		image_data = await self.bot.loop.run_in_executor(None, image_utils.resize_until_small, image_data)
		animated = image_utils.is_animated(image_data.getvalue())
		emote = await self.db.create_emote(name, author_id, animated, image_data.read())
		self.bot.dispatch('emote_add', emote)
		return emote

	@commands.command(aliases=['delete', 'delet', 'del', 'rm'])
	async def remove(self, context, *names):
		"""Removes one or more emotes from the bot. You must own all of them."""
		if not names:
			return await context.send(_('Error: you must provide the name of at least one emote to remove'))
		messages = []

		async with context.typing():
			for name in names:
				try:
					emote = await self.db.get_emote(name)
				except errors.EmoteNotFoundError as ex:
					messages.append(str(ex))
					continue

				# log the emote removal *first* because if we were to do it afterwards,
				# the emote would not display (since it's already removed)
				removal_message = await self.logger.on_emote_remove(emote)
				try:
					await self.db.remove_emote(emote, context.author.id)
				except (errors.ConnoisseurError, errors.DiscordError) as ex:
					messages.append(str(ex))
					# undo the log
					with contextlib.suppress(AttributeError):
						await removal_message.delete()
				else:
					messages.append(_('{emote.escaped_name()} was successfully deleted.').format(
						escaped_emote_name=emote.escaped_name()))

		message = '\n'.join(messages)
		await context.send(utils.fix_first_line(message))

	@commands.command(aliases=['mv'])
	async def rename(self, context, *args):
		"""Renames an emote. You must own it.

		Example:
		ec/rename a b
		Renames \:a: to \:b:
		"""

		if not args:
			return await context.send(_('You must specify an old name and a new name.'))

		# allow e.g. foo{bar,baz} -> rename foobar to foobaz
		if len(args) == 1:
			old_name, new_name = utils.expand_cartesian_product(args[0])
			if not new_name:
				return await context.send(_('Error: you must provide a new name for the emote.'))
		else:
			old_name, new_name, *_ = args

		old_name, new_name = map(lambda c: c.strip(':;'), (old_name, new_name))

		try:
			await self.db.rename_emote(old_name, new_name, context.author.id)
		except discord.HTTPException as ex:
			await context.send(utils.format_http_exception(ex))
		else:
			await context.send(_('Emote successfully renamed.'))

	@commands.command()
	async def describe(self, context, name, *, description=None):
		"""Set an emote's description. It will be displayed in ec/info.

		If you leave out the description, it will be removed.
		You could use this to:
		- Detail where you got the image
		- Credit another author
		- Write about why you like the emote
		- Describe how it's used
		Currently, there's a limit of 500 characters.
		"""
		await self.db.set_emote_description(name, context.author.id, description)
		await context.try_add_reaction(utils.SUCCESS_EMOTES[True])

	@commands.command()
	@checks.not_blacklisted()
	async def react(self, context, emote: DatabaseEmote, *, message: utils.converter.Message = None):
		"""Add a reaction to a message. Sad reacts only please.

		To specify the message, either provide a keyword to search for, or a message ID.
		If you don't specify a message, the last message sent in this channel will be chosen.
		Otherwise, the first message matching the keyword will be reacted to.
		"""

		if not await self._check_reaction_permissions(context):
			return

		if message is None:
			# get the second to last message (ie ignore the invoking message)
			message = await utils.get_message_by_offset(context.channel, -2)

		# there's no need to react to a message if that reaction already exists
		def same_emote(reaction):
			return getattr(reaction.emoji, 'id', None) == emote.id

		if discord.utils.find(same_emote, message.reactions):
			return await context.send(_(
				'You can already react to that message with that emote.'),
				delete_after=5)

		try:
			await message.add_reaction(emote.as_reaction())
		except discord.Forbidden:
			return await context.send(_('Unable to react: permission denied.'))
		except discord.HTTPException:
			return await context.send(_('Unable to react. Discord must be acting up.'))

		instruction_message = await context.send(_(
			"OK! I've reacted to that message. "
			"Please add your reaction now."))

		def check(payload):
			return (
				payload.message_id == message.id
				and payload.user_id == context.message.author.id
				and emote.id == getattr(payload.emoji, 'id', None))  # unicode emoji have no id

		try:
			await self.bot.wait_for('raw_reaction_add', timeout=30, check=check)
		except asyncio.TimeoutError:
			pass
		else:
			await self.db.log_emote_use(emote.id, context.author.id)
		finally:
			# if we don't sleep, it would appear that the bot never un-reacted
			# i.e. the reaction button would still say "2" even after we remove our reaction
			# in my testing, 0.2s is the minimum amount of time needed to work around this.
			# unfortunately, if you look at the list of reactions, it still says the bot reacted.
			# no amount of sleeping can fix that, in my tests.
			await asyncio.sleep(0.2)
			await message.remove_reaction(emote.as_reaction(), self.bot.user)

			for message in context.message, instruction_message:
				with contextlib.suppress(discord.HTTPException):
					await message.delete()

	async def _check_reaction_permissions(self, context):
		# author might not be a Member, even in a guild, if it's a webhook.
		if not context.guild or not isinstance(context.author, discord.Member):
			return True

		sender_permissions = context.channel.permissions_for(context.author)
		permissions = context.channel.permissions_for(context.guild.me)

		if not sender_permissions.read_message_history or not permissions.read_message_history:
		    await context.send(_('Unable to react: no permission to read message history.'))
		    return False
		if not sender_permissions.add_reactions or not permissions.add_reactions:
		    await context.send(_('Unable to react: no permission to add reactions.'))
		    return False

		return True

	@commands.command()
	async def list(self, context, *, user: discord.User = None):
		"""List all emotes the bot knows about.
		If a user is provided, the list will only contain emotes created by that user.
		"""
		processed = []

		args = []
		if user is not None:
			args.append(user.id)

		async for emote in self.db.all_emotes(*args):
			author = utils.format_user(self.bot, emote.author, mention=True)

			if emote.preserved:
				first_bit = _('{emote} (Preserved)').format(emote=emote.with_name())
			else:
				first_bit = emote.with_name()

			processed.append(_('{first_bit} — owned by **{author}**').format(**locals()))

		if not processed:
			return await context.send(_('No emotes have been created yet. Be the first!'))

		paginator = Pages(context, entries=processed)
		self.paginators.add(paginator)

		if self.bot.config['website']:
			end_path = f'/{user.id}' if user else ''
			paginator.text_message = _('Also check out the list website at {website}.').format(
				website='%s/%s' % (self.bot.config["website"], end_path))

		await paginator.begin()

	@commands.command(aliases=['find'])
	async def search(self, context, query):
		"""Search for emotes whose name contains "query"."""

		processed = []

		async for emote in self.db.search(query):
			processed.append(emote.with_name())

		if not processed:
			return await context.send(_('No results matched your query.'))

		paginator = Pages(context, entries=processed)
		self.paginators.add(paginator)
		await paginator.begin()

	@commands.command()
	async def popular(self, context):
		"""Lists popular emojis."""

		# code generously provided by @Liara#0001 under the MIT License:
		# https://gitlab.com/Pandentia/element-zero/blob/ca7d7f97e068e89334e66692922d9a8744e3e9be/element_zero/cogs/emoji.py#L364-399
		processed = []

		async for i, emote in utils.async_enumerate(self.db.popular_emotes()):
			if i == 200:
				break

			author = utils.format_user(self.bot, emote.author, mention=True)

			c = emote.usage
			multiple = '' if c == 1 else 's'

			processed.append(
				f'{emote.with_name()} '
				f'— used **{c}** time{multiple} '
				f'— owned by **{author}**')  # note: these are em dashes, not hyphens!

		if not processed:
			return await context.send(_('No emotes have been created yet. Be the first!'))

		paginator = Pages(context, entries=processed)
		self.paginators.add(paginator)
		await paginator.begin()

	@commands.command(name='steal-these', hidden=True)
	@checks.not_blacklisted()
	@utils.typing
	async def steal_these(self, context, *emotes):
		"""Steal a bunch of custom emotes."""
		if not emotes:
			return await context.send(_('You need to provide one or more custom emotes.'))

		messages = []
		for match in utils.emote.RE_CUSTOM_EMOTE.finditer(''.join(emotes)):
			animated, name, id = match.groups()
			image_url = utils.emote.url(id, animated=animated)
			messages.append(await self.add_safe(name, image_url, context.author.id))

		if not messages:
			return await context.send(_('Error: no existing custom emotes were provided.'))

		message = '\n'.join(messages)
		await context.send(utils.fix_first_line(message))

	@commands.command(hidden=True)
	@commands.is_owner()
	async def recover(self, context, message_id):
		"""recovers a decayed or removed emote from the log channel.

		message_id is the ID of the log message.
		"""

		try:
			channel = self.bot.get_cog('Logger').channel
		except AttributeError:
			return await context.send(_('Logger cog not loaded.'))

		try:
			message = await channel.get_message(message_id)
		except discord.NotFound:
			return await context.send(_('Message not found.'))

		try:
			description = message.embeds[0].description
		except IndexError:
			return await context.send(_('No embeds were found in that message.'))

		try:
			emote = utils.emote.RE_CUSTOM_EMOTE.match(description)
		except TypeError:
			return await context.send(_('No description was found in that embed.'))
		name = emote.group('name')

		try:
			url = utils.emote.url(emote.group('id'), animated=emote.group('animated'))
		except AttributeError:
			return await context.send(_("No custom emotes were found in that embed's description."))

		try:
			author = int((re.search(r'<@!?(\d{17,})>', description) or re.search('Unknown user with ID (\d{17,})', description)).group(1))
		except AttributeError:
			return await context.send(_('No author IDs were found in that embed.'))

		message = await self.add_safe(name, url, author)
		await context.send(message)

	@commands.command()
	async def toggle(self, context):
		"""Toggles the emote auto response (;name;) for you.
		This is global, ie it affects all servers you are in.

		If a guild has been set to opt in, you will need to run this command before I can respond to you.
		"""
		guild = None
		if context.guild is not None:
			guild = context.guild.id
		if await self.db.toggle_user_state(context.author.id, guild):
			action = 'in to'
		else:
			action = 'out of'
		await context.send(f'Opted {action} the emote auto response.')

	@commands.command(name='toggleserver')
	@checks.owner_or_permissions(manage_emojis=True)
	@commands.guild_only()
	async def toggle_guild(self, context):
		"""Toggle the auto response for this server.
		If you have never run this command before, this server is opt-out: the emote auto response is
		on for all users, except those who run ec/toggle.

		If this server is opt-out, the emote auto response is off for all users,
		and they must run ec/toggle before the bot will respond to them.

		Opt in mode is useful for very large servers where the bot's response would be annoying or
		would conflict with that of other bots.
		"""
		if await self.db.toggle_guild_state(context.guild.id):
			new_state = 'opt-out'
		else:
			new_state = 'opt-in'
		await context.send(f'Emote auto response is now {new_state} for this server.')

	@commands.command()
	@commands.is_owner()
	async def blacklist(self, context, user: discord.Member, *, reason=None):
		"""Prevent a user from using commands and the emote auto response.
		If you don't provide a reason, the user will be un-blacklisted."""
		await self.db.set_user_blacklist(user.id, reason)
		if reason is None:
			await context.send(_('User un-blacklisted.'))
		else:
			await context.send(_('User blacklisted with reason `{reason}`.').format(**locals()))

	@commands.command(hidden=True)
	@commands.is_owner()
	async def preserve(self, context, should_preserve: bool, *names):
		"""Sets preservation status of emotes."""
		names = set(names)
		for name in names:
			try:
				emote = await self.db.set_emote_preservation(name, should_preserve)
			except errors.EmoteNotFoundError as ex:
				await context.send(ex)
			else:
				self.bot.dispatch(f'emote_{"un" if not should_preserve else ""}preserve', emote)
		await context.send(utils.SUCCESS_EMOTES[True])

	## Events

	async def on_command_error(self, context, error):
		if isinstance(error, errors.ConnoisseurError):
			await context.send(error)

	async def on_message(self, message):
		"""Reply to messages containing :name: or ;name; with the corresponding emotes.
		This is like half the functionality of the bot
		"""

		if not self.bot.should_reply(message):
			return

		context = await self.bot.get_context(message)
		if context.valid:
			# user invoked a command, rather than the emote auto response
			# so don't respond a second time
			return

		if message.guild and not message.guild.me.permissions_in(message.channel).external_emojis:
			return

		if message.guild:
			guild = message.guild.id
		else:
			guild = None

		await self.bot.db_ready.wait()

		if not await self.db.get_state(guild, message.author.id):
			return

		blacklist_reason = await self.db.get_user_blacklist(message.author.id)
		if blacklist_reason is not None:
			return

		reply, has_emotes = await self.extract_emotes(message, log_usage=True)
		if not has_emotes:
			return

		self.replies[message.id] = await message.channel.send(reply)

	async def on_raw_message_edit(self, payload):
		"""Ensure that when a message containing emotes is edited, the corresponding emote reply is, too."""
		# data = https://discordapp.com/developers/docs/resources/channel#message-object
		if payload.message_id not in self.replies or 'content' not in payload.data:
			return

		message = discord.Message(
			state=self.bot._connection,
			channel=self.bot.get_channel(int(payload.data['channel_id'])),
			data=payload.data)

		emotes, message_has_emotes = await self.extract_emotes(message, log_usage=False)
		reply = self.replies[payload.message_id]
		if not message_has_emotes:
			del self.replies[payload.message_id]
			return await reply.delete()
		elif emotes == reply.content:
			# don't edit a message if we don't need to
			return

		await reply.edit(content=emotes)

	async def _extract_emotes(self,
		message: discord.Message,
		content: str = None,
		*,
		predicate,
		log_usage=False
	):
		"""Extract emotes according to predicate.
		Predicate is a function taking three arguments: token, and out: StringIO,
		and returning a boolean. Out can be written to to affect the output of this function.

		If not predicate(...), skip that token.

		Returns extracted_message: str, has_emotes: bool.
		"""

		out = io.StringIO()
		emotes_used = set()

		if content is None:
			content = message.content

		# we make a new one each time otherwise two tasks might use the same lexer at the same time
		lexer = utils.lexer()

		lexer.input(content)
		for toke1 in iter(lexer.token, None):
			if not predicate(toke1, out):
				continue

			try:
				emote = await self.db.get_emote(toke1.value.strip(':;'))
			except errors.EmoteNotFoundError:
				out.write(toke1.value)
			else:
				out.write(str(emote))
				emotes_used.add(emote.id)

		result = out.getvalue() if emotes_used else content

		if log_usage:
			for emote in emotes_used:
				await self.db.log_emote_use(emote, message.author.id)

		return utils.clean_content(self.bot, message, result), bool(emotes_used)

	async def extract_emotes(self, message: discord.Message, content: str = None, *, log_usage=False):
		"""Parse all emotes (:name: or ;name;) from a message"""
		def predicate(toke1, out):
			if toke1.type == 'TEXT' and toke1.value == '\n':
				out.write(toke1.value)
				return False

			return toke1.type == 'EMOTE'

		extracted, has_emotes = await self._extract_emotes(
			message,
			content,
			predicate=predicate,
			log_usage=log_usage)

		return utils.fix_first_line(extracted), has_emotes

	async def quote_emotes(self, message: discord.Message, content: str = None, *, log_usage=False):
		"""Parse all emotes (:name: or ;name;) from a message, preserving non-emote text"""
		def predicate(toke1, out):
			if toke1.type != 'EMOTE':
				out.write(toke1.value)
				return False

			return True

		return await self._extract_emotes(message, content, predicate=predicate, log_usage=log_usage)

	async def delete_reply(self, message_id):
		"""Delete our reply to a message containing emotes."""
		try:
			message = self.replies.pop(message_id)
		except KeyError:
			return

		with contextlib.suppress(discord.HTTPException):
			await message.delete()

	async def on_raw_message_delete(self, payload):
		"""Ensure that when a message containing emotes is deleted, the emote reply is, too."""
		await self.delete_reply(payload.message_id)

	async def on_raw_bulk_message_delete(self, payload):
		for message_id in payload.message_ids:
			await self.delete_reply(message_id)


def setup(bot):
	bot.add_cog(Emotes(bot))