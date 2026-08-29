import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import time
import json
from pathlib import Path
from collections import defaultdict, deque

import os

TOKEN = os.getenv("ANTI_TOKEN")

if not TOKEN:
    raise RuntimeError("ANTI_TOKEN не найден в Environment Variables")

BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_DIR = BASE_DIR / "snapshots"
LEGACY_SNAPSHOT_FILE = BASE_DIR / "snapshot.json"
AUTO_ROLES_FILE = BASE_DIR / "auto_roles.json"

# Runtime state is isolated by guild ID, so one server cannot affect another.
snapshot_data = {}
auto_member_roles = {}

TRUSTED_USER_IDS = {
    689455809612349463,
    1542308710905286716,
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

class AntiNukeBot(commands.Bot):
    async def setup_hook(self):
        synced = await self.tree.sync()
        print(f"[SLASH] Synced commands: {len(synced)}", flush=True)


bot = AntiNukeBot(command_prefix="!", intents=intents)

events = defaultdict(lambda: defaultdict(deque))
neutralized = set()

def protected_guild(guild):
    return guild is not None

def trusted(user_id, guild=None):
    if user_id is None:
        return False
    if bot.user and user_id == bot.user.id:
        return True
    if guild is not None and user_id == guild.owner_id:
        return True
    return user_id in TRUSTED_USER_IDS

def add_event(guild_id, user_id, event_name):
    now = time.monotonic()
    q = events[(guild_id, user_id)][event_name]
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
    if trusted(user_id, guild):
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

        neutralized.add((guild.id, user_id))

        print(f"[ANTI-NUKE] BANNED: {member}")
        print(f"[ANTI-NUKE] NEUTRALIZED: {user_id}")

        return True

    except discord.Forbidden as e:
        print(f"[ANTI-NUKE] BAN FORBIDDEN: {e}")

    except discord.HTTPException as e:
        print(f"[ANTI-NUKE] BAN HTTP ERROR: {e}")

    return False

async def register(guild, user_id, event_name):
    if user_id is None or trusted(user_id, guild):
        return False

    count = add_event(guild.id, user_id, event_name)
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


def snapshot_file(guild_id):
    return SNAPSHOT_DIR / f"{guild_id}.json"


def save_snapshot(guild_id, data):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_file(guild_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def load_snapshot(guild_id):
    path = snapshot_file(guild_id)

    # One-time compatibility with the old single-server snapshot.
    if not path.exists() and LEGACY_SNAPSHOT_FILE.exists():
        try:
            legacy = json.loads(
                LEGACY_SNAPSHOT_FILE.read_text(encoding="utf-8")
            )
            if legacy.get("guild", {}).get("id") == guild_id:
                snapshot_data[guild_id] = legacy
                save_snapshot(guild_id, legacy)
                return legacy
        except (OSError, json.JSONDecodeError) as e:
            print(f"[SNAPSHOT LEGACY LOAD ERROR] guild={guild_id}: {e}")

    if not path.exists():
        snapshot_data.pop(guild_id, None)
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("guild", {}).get("id") != guild_id:
            raise ValueError("snapshot guild ID mismatch")
        snapshot_data[guild_id] = data
        return data
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"[SNAPSHOT LOAD ERROR] guild={guild_id}: {e}")
        snapshot_data.pop(guild_id, None)
        return None


def load_auto_roles():
    auto_member_roles.clear()
    if not AUTO_ROLES_FILE.exists():
        return

    try:
        raw = json.loads(AUTO_ROLES_FILE.read_text(encoding="utf-8"))
        for guild_id, role_id in raw.items():
            if str(guild_id).isdigit() and str(role_id).isdigit():
                auto_member_roles[int(guild_id)] = int(role_id)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[AUTO ROLE LOAD ERROR] {e}")


def save_auto_roles():
    AUTO_ROLES_FILE.write_text(
        json.dumps(
            {str(guild_id): role_id for guild_id, role_id in auto_member_roles.items()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


async def make_snapshot(guild):
    data = {
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

    snapshot_data[guild.id] = data
    save_snapshot(guild.id, data)
    return len(data["channels"]), len(data["roles"])


async def restore_from_snapshot(guild):
    data = snapshot_data.get(guild.id) or load_snapshot(guild.id)

    if not data:
        raise RuntimeError("snapshot.json отсутствует или пуст")

    if data.get("guild", {}).get("id") != guild.id:
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

    for role_data in sorted(
        data.get("roles", {}).values(),
        key=lambda x: x.get("position", 0)
    ):
        role = existing_roles.get(role_data["name"])

        if role is None:
            role = await guild.create_role(
                name=role_data["name"],
                permissions=discord.Permissions(role_data["permissions"]),
                colour=discord.Colour(role_data.get("colour", 0)),
                hoist=role_data.get("hoist", False),
                mentionable=role_data.get("mentionable", False),
                reason="Anti-Nuke restore",
            )
            restored_roles += 1

        role_map[str(role_data["id"])] = role

        try:
            await role.edit(
                permissions=discord.Permissions(role_data["permissions"]),
                colour=discord.Colour(role_data.get("colour", 0)),
                hoist=role_data.get("hoist", False),
                mentionable=role_data.get("mentionable", False),
                position=role_data.get("position", role.position),
                reason="Anti-Nuke restore",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    # Give restored roles back to members.
    for role_data in data.get("roles", {}).values():
        role = role_map.get(str(role_data["id"]))
        if role is None:
            continue

        for member_id in role_data.get("member_ids", []):
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
        channel_data for channel_data in data.get("channels", {}).values()
        if channel_data.get("type") == "category"
    ]

    for category_data in sorted(categories, key=lambda x: x.get("position", 0)):
        category = existing_categories.get(category_data["name"])

        if category is None:
            category = await guild.create_category(
                category_data["name"],
                reason="Anti-Nuke restore"
            )
            restored_channels += 1

        category_map[str(category_data["id"])] = category

        try:
            await category.edit(
                position=category_data.get("position", category.position)
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    # Restore text/voice channels.
    channels = [
        channel_data for channel_data in data.get("channels", {}).values()
        if channel_data.get("type") in {"text", "voice"}
    ]

    for channel_data in sorted(channels, key=lambda x: x.get("position", 0)):
        category = category_map.get(str(channel_data.get("category_id")))

        existing = None
        for ch in guild.channels:
            if ch.name != channel_data["name"]:
                continue
            if channel_data["type"] == "text" and isinstance(ch, discord.TextChannel):
                existing = ch
                break
            if channel_data["type"] == "voice" and isinstance(ch, discord.VoiceChannel):
                existing = ch
                break

        if existing is None:
            if channel_data["type"] == "text":
                existing = await guild.create_text_channel(
                    channel_data["name"],
                    category=category,
                    topic=channel_data.get("topic"),
                    nsfw=channel_data.get("nsfw", False),
                    slowmode_delay=channel_data.get("slowmode_delay", 0),
                    reason="Anti-Nuke restore",
                )
            else:
                existing = await guild.create_voice_channel(
                    channel_data["name"],
                    category=category,
                    bitrate=channel_data.get("bitrate"),
                    user_limit=channel_data.get("user_limit", 0),
                    reason="Anti-Nuke restore",
                )

            restored_channels += 1

        try:
            await existing.edit(
                category=category,
                position=channel_data.get("position", existing.position),
                reason="Anti-Nuke restore",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    old_name = data.get("guild", {}).get("name")
    if old_name and guild.name != old_name:
        try:
            await guild.edit(name=old_name, reason="Anti-Nuke restore")
        except (discord.Forbidden, discord.HTTPException):
            pass

    return restored_channels, restored_roles


@bot.event
async def on_ready():
    activity = discord.Activity(
        type=discord.ActivityType.watching,
        name="LFR Community",
    )
    await bot.change_presence(activity=activity)

    load_auto_roles()
    for guild in bot.guilds:
        load_snapshot(guild.id)

    print("=" * 60)
    print(f"ANTI-NUKE ONLINE: {bot.user}")
    print(f"PROTECTED GUILDS: {len(bot.guilds)}")
    for guild in bot.guilds:
        print(f"- {guild.name} ({guild.id})")
    print("=" * 60)


@bot.event
async def on_guild_join(guild):
    load_snapshot(guild.id)
    print(f"[ANTI-NUKE] Added to guild: {guild.name} ({guild.id})")

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

    if dangerous and not trusted(user_id, role.guild):
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

    elif triggered and not trusted(user_id, role.guild):
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

    if gained_dangerous and not trusted(user_id, after.guild):
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

    if trusted(user_id, after.guild):
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
    if not protected_guild(after):
        return

    if before.name == after.name:
        return

    user_id = await find_executor(
        after,
        discord.AuditLogAction.guild_update,
        after.id
    )

    if trusted(user_id, after):
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
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.event
async def on_member_join(member):
    if not protected_guild(member.guild):
        return

    role_id = auto_member_roles.get(member.guild.id)
    if role_id is None:
        return

    role = member.guild.get_role(role_id)

    if role is None:
        print(
            f"[AUTO ROLE] Role {role_id} not found "
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
        if message.author.bot and not trusted(message.author.id, message.guild):
            count = add_event(
                message.guild.id,
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


def is_interaction_admin(interaction):
    return (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


def make_status_embed(guild):
    data = snapshot_data.get(guild.id) or load_snapshot(guild.id) or {}
    auto_role_id = auto_member_roles.get(guild.id)

    embed = discord.Embed(
        title="🛡️ LFR Anti-Nuke",
        description="Защита сервера активна.",
        color=discord.Color.from_rgb(88, 101, 242),
    )
    embed.add_field(name="Сервер", value=f"{guild.name}\n`{guild.id}`", inline=False)
    embed.add_field(
        name="Snapshot",
        value=(
            f"`{'YES' if data else 'NO'}`\n"
            f"Каналы: `{len(data.get('channels', {}))}`\n"
            f"Роли: `{len(data.get('roles', {}))}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="Auto Role",
        value=f"<@&{auto_role_id}>" if auto_role_id else "`OFF`",
        inline=True,
    )
    embed.add_field(
        name="Лимиты",
        value=(
            f"Окно: `{WINDOW_SECONDS}s`\n"
            f"Удаление каналов: `{LIMITS['channel_delete']}`\n"
            f"Создание каналов: `{LIMITS['channel_create']}`\n"
            f"Удаление ролей: `{LIMITS['role_delete']}`\n"
            f"Создание ролей: `{LIMITS['role_create']}`"
        ),
        inline=False,
    )
    embed.set_footer(text="LFR Community • Anti-Nuke")
    return embed


async def reset_member_roles(guild, target_role):
    me = guild.me
    if me is None:
        raise RuntimeError("Не удалось определить роль anti-nuke бота")
    if target_role >= me.top_role:
        raise RuntimeError(
            "Роль для выдачи находится выше или на одном уровне с ролью бота"
        )

    processed = 0
    cleaned = 0
    assigned = 0
    failed = 0

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
            and role.id != target_role.id
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
                f"[ROLES RESET] guild={guild.id} {member} | "
                f"removed={len(removable_roles)} | "
                f"target={'yes' if target_role in member.roles else 'added'}"
            )
        except discord.Forbidden as e:
            failed += 1
            print(f"[ROLES RESET] FORBIDDEN {member}: {e}")
        except discord.HTTPException as e:
            failed += 1
            print(f"[ROLES RESET] HTTP ERROR {member}: {e}")

    return processed, cleaned, assigned, failed

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

    await ctx.send(embed=make_status_embed(ctx.guild))


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


@bot.command(name="autorole")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def autorole_command(ctx, *, value="status"):
    normalized = value.strip().lower()

    if normalized in {"status", "show"}:
        role_id = auto_member_roles.get(ctx.guild.id)
        if role_id is None:
            await ctx.send("ℹ️ Авто-роль отключена. Настройка: `!autorole @роль`")
        else:
            await ctx.send(f"✅ Текущая авто-роль: <@&{role_id}> (`{role_id}`)")
        return

    if normalized in {"off", "disable", "none"}:
        auto_member_roles.pop(ctx.guild.id, None)
        save_auto_roles()
        await ctx.send("✅ Авто-роль отключена для этого сервера")
        return

    try:
        role = await commands.RoleConverter().convert(ctx, value)
    except commands.BadArgument:
        await ctx.send("❌ Укажи роль: `!autorole @роль`, либо `!autorole off`")
        return

    me = ctx.guild.me
    if role.is_default() or role.managed:
        await ctx.send("❌ Эту роль нельзя использовать как авто-роль")
        return
    if me is None or role >= me.top_role:
        await ctx.send("❌ Роль должна находиться ниже роли бота")
        return

    auto_member_roles[ctx.guild.id] = role.id
    save_auto_roles()
    await ctx.send(f"✅ Авто-роль для этого сервера: {role.mention}")


@autorole_command.error
async def autorole_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Для `!autorole` нужны права Administrator")
    elif not isinstance(error, commands.NoPrivateMessage):
        print(f"[AUTO ROLE COMMAND ERROR] {error}")


@bot.command(name="rolesreset")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def rolesreset_command(ctx):
    if not protected_guild(ctx.guild):
        await ctx.send("❌ Это не защищаемый сервер")
        return

    guild = ctx.guild
    target_role_id = auto_member_roles.get(guild.id)
    if target_role_id is None:
        await ctx.send(
            "❌ Сначала настрой роль для этого сервера: `!autorole @роль`"
        )
        return

    target_role = guild.get_role(target_role_id)

    if target_role is None:
        await ctx.send(
            f"❌ Роль `{target_role_id}` не найдена на сервере"
        )
        return

    await ctx.send(
        f"🔄 Начинаю reset ролей для обычных пользователей\n"
        f"Будет выдана роль: <@&{target_role_id}>"
    )

    try:
        processed, cleaned, assigned, failed = await reset_member_roles(
            guild, target_role
        )
    except RuntimeError as e:
        await ctx.send(f"❌ {e}")
        return

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


@bot.tree.command(name="help", description="Показать справку LFR Anti-Nuke")
@app_commands.guild_only()
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛡️ LFR Anti-Nuke — команды",
        description=(
            "Бот автоматически защищает каждый сервер, на котором находится. "
            "Настройки и snapshots у каждого сервера отдельные."
        ),
        color=discord.Color.from_rgb(88, 101, 242),
    )
    embed.add_field(
        name="ℹ️ Основные",
        value=(
            "`/help` — эта справка\n"
            "`/status` — состояние защиты сервера\n"
            "`/ping` — задержка бота"
        ),
        inline=False,
    )
    embed.add_field(
        name="💾 Snapshot",
        value=(
            "`/snapshot` — сохранить каналы и роли\n"
            "`/restore` — восстановить сервер из snapshot"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Настройка",
        value=(
            "`/autorole` — статус авто-роли\n"
            "`/autorole role:@роль` — настроить авто-роль\n"
            "`/autorole disable:True` — отключить авто-роль"
        ),
        inline=False,
    )
    embed.add_field(
        name="🚨 Экстренное управление",
        value=(
            "`/rolesreset confirm:True` — снять доступные роли у участников "
            "и выдать настроенную авто-роль"
        ),
        inline=False,
    )
    embed.set_footer(text="Команды настройки доступны администраторам")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Показать состояние защиты сервера")
@app_commands.guild_only()
async def slash_status(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=make_status_embed(interaction.guild),
        ephemeral=True,
    )


@bot.tree.command(name="ping", description="Показать задержку LFR Anti-Nuke")
@app_commands.guild_only()
async def slash_ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(
        f"✅ Pong: `{latency} ms` | Server: `{interaction.guild.id}`",
        ephemeral=True,
    )


@bot.tree.command(name="snapshot", description="Сохранить каналы и роли сервера")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def slash_snapshot(interaction: discord.Interaction):
    if not is_interaction_admin(interaction):
        await interaction.response.send_message(
            "❌ Нужны права Administrator.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        channels_count, roles_count = await make_snapshot(interaction.guild)
        await interaction.followup.send(
            "✅ Snapshot сохранён\n"
            f"Каналов: `{channels_count}`\n"
            f"Ролей: `{roles_count}`",
            ephemeral=True,
        )
    except (OSError, discord.Forbidden, discord.HTTPException) as e:
        print(f"[SNAPSHOT ERROR] {type(e).__name__}: {e}")
        await interaction.followup.send(
            f"❌ Snapshot error: `{type(e).__name__}`", ephemeral=True
        )


@bot.tree.command(name="restore", description="Восстановить сервер из snapshot")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def slash_restore(interaction: discord.Interaction):
    if not is_interaction_admin(interaction):
        await interaction.response.send_message(
            "❌ Нужны права Administrator.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        channels_count, roles_count = await restore_from_snapshot(interaction.guild)
        await interaction.followup.send(
            "✅ Restore завершён\n"
            f"Восстановлено каналов: `{channels_count}`\n"
            f"Восстановлено ролей: `{roles_count}`",
            ephemeral=True,
        )
    except (RuntimeError, OSError, discord.Forbidden, discord.HTTPException) as e:
        print(f"[RESTORE ERROR] {type(e).__name__}: {e}")
        await interaction.followup.send(
            f"❌ Restore error: `{type(e).__name__}: {e}`", ephemeral=True
        )


@bot.tree.command(name="autorole", description="Настроить авто-роль сервера")
@app_commands.describe(
    role="Роль для новых участников; оставь пустой для просмотра",
    disable="Отключить авто-роль",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def slash_autorole(
    interaction: discord.Interaction,
    role: discord.Role | None = None,
    disable: bool = False,
):
    if not is_interaction_admin(interaction):
        await interaction.response.send_message(
            "❌ Нужны права Administrator.", ephemeral=True
        )
        return

    if disable:
        auto_member_roles.pop(interaction.guild.id, None)
        save_auto_roles()
        await interaction.response.send_message(
            "✅ Авто-роль отключена для этого сервера.", ephemeral=True
        )
        return

    if role is None:
        role_id = auto_member_roles.get(interaction.guild.id)
        text = (
            f"✅ Текущая авто-роль: <@&{role_id}> (`{role_id}`)"
            if role_id
            else "ℹ️ Авто-роль отключена. Укажи параметр `role`."
        )
        await interaction.response.send_message(text, ephemeral=True)
        return

    me = interaction.guild.me
    if role.is_default() or role.managed:
        await interaction.response.send_message(
            "❌ Эту роль нельзя использовать как авто-роль.", ephemeral=True
        )
        return
    if me is None or role >= me.top_role:
        await interaction.response.send_message(
            "❌ Роль должна находиться ниже роли бота.", ephemeral=True
        )
        return

    auto_member_roles[interaction.guild.id] = role.id
    save_auto_roles()
    await interaction.response.send_message(
        f"✅ Авто-роль для этого сервера: {role.mention}", ephemeral=True
    )


@bot.tree.command(
    name="rolesreset",
    description="Экстренно сбросить роли обычных участников",
)
@app_commands.describe(confirm="Подтверждение необратимого массового изменения ролей")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def slash_rolesreset(interaction: discord.Interaction, confirm: bool):
    if not is_interaction_admin(interaction):
        await interaction.response.send_message(
            "❌ Нужны права Administrator.", ephemeral=True
        )
        return
    if not confirm:
        await interaction.response.send_message(
            "❌ Операция отменена. Для запуска выбери `confirm: True`.",
            ephemeral=True,
        )
        return

    target_role_id = auto_member_roles.get(interaction.guild.id)
    target_role = (
        interaction.guild.get_role(target_role_id) if target_role_id else None
    )
    if target_role is None:
        await interaction.response.send_message(
            "❌ Сначала настрой авто-роль через `/autorole`.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        processed, cleaned, assigned, failed = await reset_member_roles(
            interaction.guild, target_role
        )
    except RuntimeError as e:
        await interaction.followup.send(f"❌ {e}", ephemeral=True)
        return

    await interaction.followup.send(
        "✅ Role reset завершён\n"
        f"Участников: `{processed}`\n"
        f"Роли сняты у: `{cleaned}`\n"
        f"Авто-роль выдана: `{assigned}`\n"
        f"Ошибок: `{failed}`",
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    original = getattr(error, "original", error)
    print(
        f"[SLASH ERROR] command={getattr(interaction.command, 'name', None)} "
        f"user={interaction.user.id} guild={interaction.guild_id} "
        f"error={type(original).__name__}: {original}"
    )
    message = "❌ Не удалось выполнить команду. Посмотри логи бота."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


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

if __name__ == "__main__":
    bot.run(TOKEN)
