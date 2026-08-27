import discord
from discord.ext import commands
import asyncio
import time
import json
from pathlib import Path
from collections import defaultdict, deque

import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("ANTI_TOKEN")

if not TOKEN:
    raise RuntimeError("ANTI_TOKEN не найден в .env")
PROTECTED_GUILD_ID = 1531068685576572978
SNAPSHOT_FILE = Path("snapshot.json")
snapshot_data = {}
AUTO_MEMBER_ROLE_ID = 1542492970413199411

TRUSTED_USER_IDS = {
    689455809612349463
}

WINDOW_SECONDS = 6

LIMITS = {
    "channel_delete": 2,
    "channel_create": 5,
    "role_delete": 2,
    "role_create": 2,
    "role_update": 3,
    "member_role_grant": 5,
    "guild_update": 1,
    "message_spam": 8,
}

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

events = defaultdict(lambda: defaultdict(deque))
neutralized = set()
last_known_guild_name = None

def protected_guild(guild):
    return guild is not None and guild.id == PROTECTED_GUILD_ID

def trusted(user_id):
    if user_id is None:
        return False
    if bot.user and user_id == bot.user.id:
        return True
    return user_id in TRUSTED_USER_IDS

def add_event(user_id, event_name):
    now = time.monotonic()
    q = events[user_id][event_name]
    q.append(now)

    while q and now - q[0] > WINDOW_SECONDS:
        q.popleft()

    return len(q)

async def find_executor(guild, action, target_id=None):
    await asyncio.sleep(0.15)

    try:
        async for entry in guild.audit_logs(limit=8, action=action):
            if entry.user_id is None:
                continue

            if target_id is not None:
                entry_target_id = getattr(entry.target, "id", None)
                if entry_target_id != target_id:
                    continue

            return entry.user_id
    except (discord.Forbidden, discord.HTTPException):
        return None

    return None

async def strip_member_roles(member):
    guild = member.guild
    me = guild.me

    if me is None:
        print("[ANTI-NUKE] ERROR: guild.me is None")
        return False

    print(
        f"[ANTI-NUKE] Defender top role: {me.top_role.name} "
        f"pos={me.top_role.position}"
    )
    print(
        f"[ANTI-NUKE] Attacker top role: {member.top_role.name} "
        f"pos={member.top_role.position}"
    )

    removable = []

    for role in member.roles:
        if role.is_default() or role.managed:
            continue

        if role >= me.top_role:
            print(
                f"[ANTI-NUKE] CANNOT REMOVE ROLE: "
                f"{role.name} pos={role.position}"
            )
            continue

        removable.append(role)

    if not removable:
        print("[ANTI-NUKE] No removable roles found")
        return False

    try:
        await member.remove_roles(
            *removable,
            reason="Anti-Nuke protection triggered"
        )
        print(
            "[ANTI-NUKE] Removed roles: "
            + ", ".join(role.name for role in removable)
        )
        return True

    except discord.Forbidden as e:
        print(f"[ANTI-NUKE] REMOVE ROLES FORBIDDEN: {e}")
        return False

    except discord.HTTPException as e:
        print(f"[ANTI-NUKE] REMOVE ROLES HTTP ERROR: {e}")
        return False

async def neutralize(guild, user_id, reason):
    if trusted(user_id):
        return False

    member = guild.get_member(user_id)

    print(f"[ANTI-NUKE] TRIGGERED: {user_id} | {reason}")

    if member is None:
        print("[ANTI-NUKE] ERROR: attacker member not found in guild cache")
        return False

    try:
        await guild.ban(
            member,
            reason=f"Anti-Nuke: {reason}",
            delete_message_seconds=0
        )

        neutralized.add(user_id)

        print(f"[ANTI-NUKE] BANNED: {member}")
        print(f"[ANTI-NUKE] NEUTRALIZED: {user_id}")

        return True

    except discord.Forbidden as e:
        print(f"[ANTI-NUKE] BAN FORBIDDEN: {e}")

    except discord.HTTPException as e:
        print(f"[ANTI-NUKE] BAN HTTP ERROR: {e}")

    return False

async def register(guild, user_id, event_name):
    if user_id is None or trusted(user_id):
        return False

    count = add_event(user_id, event_name)
    limit = LIMITS[event_name]

    print(
        f"[ANTI-NUKE] {event_name}: "
        f"user={user_id} count={count}/{limit}"
    )

    if count >= limit:
        await neutralize(
            guild,
            user_id,
            f"{event_name} threshold {count}/{limit}"
        )
        return True

    return False


def channel_snapshot(channel):
    data = {
        "id": channel.id,
        "name": channel.name,
        "position": channel.position,
        "category_id": channel.category_id,
    }

    if isinstance(channel, discord.CategoryChannel):
        data["type"] = "category"
    elif isinstance(channel, discord.TextChannel):
        data.update({
            "type": "text",
            "topic": channel.topic,
            "nsfw": channel.nsfw,
            "slowmode_delay": channel.slowmode_delay,
        })
    elif isinstance(channel, discord.VoiceChannel):
        data.update({
            "type": "voice",
            "bitrate": channel.bitrate,
            "user_limit": channel.user_limit,
        })
    else:
        data["type"] = "unknown"

    return data


def role_snapshot(role):
    return {
        "id": role.id,
        "name": role.name,
        "permissions": role.permissions.value,
        "position": role.position,
        "colour": role.colour.value,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "member_ids": [m.id for m in role.members],
    }


def save_snapshot():
    SNAPSHOT_FILE.write_text(
        json.dumps(snapshot_data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def load_snapshot():
    global snapshot_data

    if not SNAPSHOT_FILE.exists():
        snapshot_data = {}
        return False

    try:
        snapshot_data = json.loads(
            SNAPSHOT_FILE.read_text(encoding="utf-8")
        )
        return True
    except Exception as e:
        print(f"[SNAPSHOT LOAD ERROR] {e}")
        snapshot_data = {}
        return False


async def make_snapshot(guild):
    global snapshot_data

    snapshot_data = {
        "guild": {
            "id": guild.id,
            "name": guild.name,
        },
        "channels": {
            str(ch.id): channel_snapshot(ch)
            for ch in guild.channels
        },
        "roles": {
            str(role.id): role_snapshot(role)
            for role in guild.roles
            if not role.is_default() and not role.managed
        },
    }

    save_snapshot()
    return len(snapshot_data["channels"]), len(snapshot_data["roles"])


async def restore_from_snapshot(guild):
    if not snapshot_data and not load_snapshot():
        raise RuntimeError("snapshot.json отсутствует или пуст")

    if snapshot_data.get("guild", {}).get("id") != guild.id:
        raise RuntimeError("Snapshot принадлежит другому серверу")

    restored_roles = 0
    restored_channels = 0
    role_map = {}
    category_map = {}

    # Roles first, so channel/category permissions can refer to them later.
    existing_roles = {
        role.name: role
        for role in guild.roles
        if not role.is_default() and not role.managed
    }

    for data in sorted(
        snapshot_data.get("roles", {}).values(),
        key=lambda x: x.get("position", 0)
    ):
        role = existing_roles.get(data["name"])

        if role is None:
            role = await guild.create_role(
                name=data["name"],
                permissions=discord.Permissions(data["permissions"]),
                colour=discord.Colour(data.get("colour", 0)),
                hoist=data.get("hoist", False),
                mentionable=data.get("mentionable", False),
                reason="Anti-Nuke restore",
            )
            restored_roles += 1

        role_map[str(data["id"])] = role

        try:
            await role.edit(
                permissions=discord.Permissions(data["permissions"]),
                colour=discord.Colour(data.get("colour", 0)),
                hoist=data.get("hoist", False),
                mentionable=data.get("mentionable", False),
                position=data.get("position", role.position),
                reason="Anti-Nuke restore",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    # Give restored roles back to members.
    for data in snapshot_data.get("roles", {}).values():
        role = role_map.get(str(data["id"]))
        if role is None:
            continue

        for member_id in data.get("member_ids", []):
            member = guild.get_member(member_id)
            if member is None or role in member.roles:
                continue

            try:
                await member.add_roles(role, reason="Anti-Nuke restore")
            except (discord.Forbidden, discord.HTTPException):
                pass

    # Restore categories.
    existing_categories = {c.name: c for c in guild.categories}

    categories = [
        data for data in snapshot_data.get("channels", {}).values()
        if data.get("type") == "category"
    ]

    for data in sorted(categories, key=lambda x: x.get("position", 0)):
        category = existing_categories.get(data["name"])

        if category is None:
            category = await guild.create_category(
                data["name"],
                reason="Anti-Nuke restore"
            )
            restored_channels += 1

        category_map[str(data["id"])] = category

        try:
            await category.edit(position=data.get("position", category.position))
        except (discord.Forbidden, discord.HTTPException):
            pass

    # Restore text/voice channels.
    channels = [
        data for data in snapshot_data.get("channels", {}).values()
        if data.get("type") in {"text", "voice"}
    ]

    for data in sorted(channels, key=lambda x: x.get("position", 0)):
        category = category_map.get(str(data.get("category_id")))

        existing = None
        for ch in guild.channels:
            if ch.name != data["name"]:
                continue
            if data["type"] == "text" and isinstance(ch, discord.TextChannel):
                existing = ch
                break
            if data["type"] == "voice" and isinstance(ch, discord.VoiceChannel):
                existing = ch
                break

        if existing is None:
            if data["type"] == "text":
                existing = await guild.create_text_channel(
                    data["name"],
                    category=category,
                    topic=data.get("topic"),
                    nsfw=data.get("nsfw", False),
                    slowmode_delay=data.get("slowmode_delay", 0),
                    reason="Anti-Nuke restore",
                )
            else:
                existing = await guild.create_voice_channel(
                    data["name"],
                    category=category,
                    bitrate=data.get("bitrate"),
                    user_limit=data.get("user_limit", 0),
                    reason="Anti-Nuke restore",
                )

            restored_channels += 1

        try:
            await existing.edit(
                category=category,
                position=data.get("position", existing.position),
                reason="Anti-Nuke restore",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    old_name = snapshot_data.get("guild", {}).get("name")
    if old_name and guild.name != old_name:
        try:
            await guild.edit(name=old_name, reason="Anti-Nuke restore")
        except (discord.Forbidden, discord.HTTPException):
            pass

    return restored_channels, restored_roles


@bot.event
async def on_ready():
    global last_known_guild_name

    load_snapshot()

    guild = bot.get_guild(PROTECTED_GUILD_ID)

    if guild is not None:
        last_known_guild_name = guild.name

    print("=" * 60)
    print(f"ANTI-NUKE ONLINE: {bot.user}")
    print(f"PROTECTED GUILD: {PROTECTED_GUILD_ID}")
    print("=" * 60)

@bot.event
async def on_guild_channel_delete(channel):
    if not protected_guild(channel.guild):
        return

    user_id = await find_executor(
        channel.guild,
        discord.AuditLogAction.channel_delete,
        channel.id
    )

    await register(
        channel.guild,
        user_id,
        "channel_delete"
    )

@bot.event
async def on_guild_channel_create(channel):
    if not protected_guild(channel.guild):
        return

    user_id = await find_executor(
        channel.guild,
        discord.AuditLogAction.channel_create,
        channel.id
    )

    triggered = await register(
        channel.guild,
        user_id,
        "channel_create"
    )

    if triggered and user_id is not None:
        try:
            await channel.delete(
                reason="Anti-Nuke cleanup"
            )
        except discord.Forbidden as e:
            print(f"[ANTI-NUKE] CLEANUP FORBIDDEN: {e}")
        except discord.HTTPException as e:
            print(f"[ANTI-NUKE] CLEANUP HTTP ERROR: {e}")

@bot.event
async def on_guild_role_delete(role):
    if not protected_guild(role.guild):
        return

    user_id = await find_executor(
        role.guild,
        discord.AuditLogAction.role_delete,
        role.id
    )

    await register(
        role.guild,
        user_id,
        "role_delete"
    )

@bot.event
async def on_guild_role_create(role):
    if not protected_guild(role.guild):
        return

    user_id = await find_executor(
        role.guild,
        discord.AuditLogAction.role_create,
        role.id
    )

    dangerous = (
        role.permissions.administrator
        or role.permissions.manage_guild
        or role.permissions.manage_roles
        or role.permissions.ban_members
        or role.permissions.kick_members
    )

    triggered = await register(
        role.guild,
        user_id,
        "role_create"
    )

    if dangerous and not trusted(user_id):
        await neutralize(
            role.guild,
            user_id,
            "dangerous role created"
        )

        try:
            await role.delete(
                reason="Anti-Nuke dangerous role cleanup"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    elif triggered and not trusted(user_id):
        try:
            await role.delete(
                reason="Anti-Nuke role cleanup"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

@bot.event
async def on_guild_role_update(before, after):
    if not protected_guild(after.guild):
        return

    user_id = await find_executor(
        after.guild,
        discord.AuditLogAction.role_update,
        after.id
    )

    await register(
        after.guild,
        user_id,
        "role_update"
    )

    gained_dangerous = (
        not before.permissions.administrator
        and after.permissions.administrator
    )

    if gained_dangerous and not trusted(user_id):
        await neutralize(
            after.guild,
            user_id,
            "administrator permission granted to role"
        )

        try:
            await after.edit(
                permissions=before.permissions,
                reason="Anti-Nuke rollback"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

@bot.event
async def on_member_update(before, after):
    if not protected_guild(after.guild):
        return

    before_ids = {role.id for role in before.roles}
    added_roles = [
        role
        for role in after.roles
        if role.id not in before_ids
    ]

    if not added_roles:
        return

    user_id = await find_executor(
        after.guild,
        discord.AuditLogAction.member_role_update,
        after.id
    )

    if trusted(user_id):
        return

    for role in added_roles:
        dangerous = (
            role.permissions.administrator
            or role.permissions.manage_guild
            or role.permissions.manage_roles
            or role.permissions.ban_members
            or role.permissions.kick_members
        )

        await register(
            after.guild,
            user_id,
            "member_role_grant"
        )

        if dangerous:
            await neutralize(
                after.guild,
                user_id,
                "dangerous role mass-assignment"
            )

            try:
                await after.remove_roles(
                    role,
                    reason="Anti-Nuke rollback"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

@bot.event
async def on_guild_update(before, after):
    global last_known_guild_name

    if not protected_guild(after):
        return

    if before.name == after.name:
        return

    user_id = await find_executor(
        after,
        discord.AuditLogAction.guild_update,
        after.id
    )

    if trusted(user_id):
        last_known_guild_name = after.name
        return

    await register(
        after,
        user_id,
        "guild_update"
    )

    try:
        await after.edit(
            name=before.name,
            reason="Anti-Nuke rollback"
        )
        last_known_guild_name = before.name
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.event
async def on_member_join(member):
    if not protected_guild(member.guild):
        return

    role = member.guild.get_role(AUTO_MEMBER_ROLE_ID)

    if role is None:
        print(
            f"[AUTO ROLE] Role {AUTO_MEMBER_ROLE_ID} not found "
            f"in guild {member.guild.id}"
        )
        return

    me = member.guild.me

    if me is None or role >= me.top_role:
        print(
            f"[AUTO ROLE] Cannot assign {role.name}: "
            "role is above/equal to bot top role"
        )
        return

    try:
        await member.add_roles(
            role,
            reason="LFR automatic member role"
        )
        print(
            f"[AUTO ROLE] {role.name} -> "
            f"{member} ({member.id})"
        )

    except discord.Forbidden as e:
        print(f"[AUTO ROLE] FORBIDDEN: {member}: {e}")

    except discord.HTTPException as e:
        print(f"[AUTO ROLE] HTTP ERROR: {member}: {e}")


@bot.event
async def on_message(message):
    if message.author.id == bot.user.id:
        return

    if message.guild is not None and protected_guild(message.guild):
        if message.author.bot and not trusted(message.author.id):
            count = add_event(
                message.author.id,
                "message_spam"
            )

            if count >= LIMITS["message_spam"]:
                await neutralize(
                    message.guild,
                    message.author.id,
                    "bot message spam"
                )

    await bot.process_commands(message)

@bot.command(name="snapshot")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def snapshot_command(ctx):
    if not protected_guild(ctx.guild):
        await ctx.send("❌ Это не защищаемый сервер")
        return

    try:
        channels_count, roles_count = await make_snapshot(ctx.guild)

        await ctx.send(
            "✅ Snapshot сохранён\n"
            f"Каналов: `{channels_count}`\n"
            f"Ролей: `{roles_count}`"
        )
    except Exception as e:
        print(f"[SNAPSHOT ERROR] {type(e).__name__}: {e}")
        await ctx.send(f"❌ Snapshot error: `{type(e).__name__}: {e}`")


@snapshot_command.error
async def snapshot_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Для `!snapshot` нужны права Administrator")
    elif not isinstance(error, commands.NoPrivateMessage):
        print(f"[SNAPSHOT COMMAND ERROR] {error}")


@bot.command(name="status")
@commands.guild_only()
async def status(ctx):
    if not protected_guild(ctx.guild):
        await ctx.send("❌ Это не защищаемый сервер")
        return

    await ctx.send(
        "🛡️ Anti-Nuke online\n"
        f"Snapshot: `{'YES' if snapshot_data else 'NO'}`\n"
        f"Snapshot channels: `{len(snapshot_data.get('channels', {}))}`\n"
        f"Snapshot roles: `{len(snapshot_data.get('roles', {}))}`\n"
        f"Window: `{WINDOW_SECONDS}s`\n"
        f"Channel delete: `{LIMITS['channel_delete']}`\n"
        f"Channel create: `{LIMITS['channel_create']}`\n"
        f"Role delete: `{LIMITS['role_delete']}`\n"
        f"Role create: `{LIMITS['role_create']}`"
    )


@bot.command(name="restore")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def restore_command(ctx):
    if not protected_guild(ctx.guild):
        await ctx.send("❌ Это не защищаемый сервер")
        return

    try:
        channels_count, roles_count = await restore_from_snapshot(ctx.guild)

        await ctx.send(
            "✅ Restore завершён\n"
            f"Восстановлено каналов: `{channels_count}`\n"
            f"Восстановлено ролей: `{roles_count}`"
        )
    except Exception as e:
        print(f"[RESTORE ERROR] {type(e).__name__}: {e}")
        await ctx.send(f"❌ Restore error: `{type(e).__name__}: {e}`")


@restore_command.error
async def restore_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Для `!restore` нужны права Administrator")
    elif not isinstance(error, commands.NoPrivateMessage):
        print(f"[RESTORE COMMAND ERROR] {error}")


@bot.command(name="rolesreset")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def rolesreset_command(ctx):
    if not protected_guild(ctx.guild):
        await ctx.send("❌ Это не защищаемый сервер")
        return

    guild = ctx.guild
    target_role_id = AUTO_MEMBER_ROLE_ID
    target_role = guild.get_role(target_role_id)

    if target_role is None:
        await ctx.send(
            f"❌ Роль `{target_role_id}` не найдена на сервере"
        )
        return

    me = guild.me

    if me is None:
        await ctx.send("❌ Не удалось определить роль anti-nuke бота")
        return

    if target_role >= me.top_role:
        await ctx.send(
            "❌ Роль для выдачи находится выше или на одном уровне "
            "с ролью anti-nuke бота"
        )
        return

    processed = 0
    cleaned = 0
    assigned = 0
    failed = 0

    await ctx.send(
        f"🔄 Начинаю reset ролей для обычных пользователей\n"
        f"Будет выдана роль: <@&{target_role_id}>"
    )

    for member in guild.members:
        if member.bot:
            continue

        processed += 1

        removable_roles = [
            role
            for role in member.roles
            if not role.is_default()
            and not role.managed
            and role < me.top_role
            and role.id != target_role_id
        ]

        try:
            if removable_roles:
                await member.remove_roles(
                    *removable_roles,
                    reason="Anti-Nuke emergency role reset"
                )
                cleaned += 1

            if target_role not in member.roles:
                await member.add_roles(
                    target_role,
                    reason="Anti-Nuke emergency role reset"
                )
                assigned += 1

            print(
                f"[ROLES RESET] {member} | "
                f"removed={len(removable_roles)} | "
                f"target={'yes' if target_role in member.roles else 'added'}"
            )

        except discord.Forbidden as e:
            failed += 1
            print(f"[ROLES RESET] FORBIDDEN {member}: {e}")

        except discord.HTTPException as e:
            failed += 1
            print(f"[ROLES RESET] HTTP ERROR {member}: {e}")

    await ctx.send(
        "✅ Role reset завершён\n"
        f"Проверено пользователей: `{processed}`\n"
        f"Сняты роли у: `{cleaned}`\n"
        f"Целевая роль выдана: `{assigned}`\n"
        f"Ошибок: `{failed}`"
    )


@rolesreset_command.error
async def rolesreset_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Для `!rolesreset` нужны права Administrator")
    elif not isinstance(error, commands.NoPrivateMessage):
        print(f"[ROLES RESET COMMAND ERROR] {error}")


@bot.command(name="ping")
@commands.guild_only()
async def ping(ctx):
    await ctx.send(
        f"✅ Pong | guild={ctx.guild.id} | protected={protected_guild(ctx.guild)}"
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingPermissions):
        await ctx.send(f"❌ Не хватает прав: `{error}`")
        return

    if isinstance(error, commands.NoPrivateMessage):
        return

    print(
        f"[COMMAND ERROR] command={getattr(ctx.command, 'name', None)} "
        f"user={ctx.author} guild={getattr(ctx.guild, 'id', None)} "
        f"error={type(error).__name__}: {error}"
    )

    try:
        await ctx.send(
            f"❌ Ошибка команды: `{type(error).__name__}: {error}`"
        )
    except Exception:
        pass

bot.run(TOKEN)