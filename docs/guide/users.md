# Users and access

Everyone who signs in has an account, added by a site administrator. What they
can see and change depends on their role for each station.

## Roles

| | Visitor | Viewer | Advanced viewer | Manager | Owner | Administrator |
|---|---|---|---|---|---|---|
| A public station's dashboard, charts, almanac, growing and reports | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| A **private** station | | ✓ | ✓ | ✓ | ✓ | ✓ |
| Indoor readings, private sensors, private log entries | | ✓ | ✓ | ✓ | ✓ | ✓ |
| Batteries and the neighbours line | | | ✓ | ✓ | ✓ | ✓ |
| The **Manage** tabs (settings, data quality, calibration, log, neighbours, uploads), read-only | | | ✓ | ✓ | ✓ | ✓ |
| Changing them; the upload paths | | | | ✓ | ✓ | ✓ |
| Who has access, public or private, ownership | | | | | ✓ | ✓ |

- A **visitor** is anyone not signed in, or signed in without access to the
  station.
- Each station has one **owner**. **Viewers**, **advanced viewers** and
  **managers** are people the owner shares it with, on the station's **People**
  tab.
- An **advanced viewer** sees everything a manager sees, but can't change
  anything: the Manage tabs are read-only, without their forms and buttons. They
  don't see the upload paths, which work like a password for sending readings to
  the station.
- **Administrators** (site administrators) see and manage every station, and
  also have **Site settings**, **Manage stations** and **Users**.

## Adding users

Administrators add people under **Users**, in the menu under their name:

1. **Add a user**: a username, optionally a name and email, and whether they
   are a site administrator.
2. WS4Free shows a **welcome link**, once. Send it to them yourself: WS4Free
   doesn't send email. The link works once and lasts 3 days.
3. They open it, choose a password, and sign in. Like everyone, they're then
   asked to set up two-step sign-in.

A user's page shows the stations they own or can access, whether two-step
sign-in is on, and when they last signed in. From there:

- **New link**: a fresh welcome link, for a lost or expired invitation or a
  forgotten password. Using it sets a new password; the old one works until
  then.
- **Site administrator**: tick or untick. You can't remove your own
  administrator access, and the site always keeps at least one administrator.
- **Deactivate**: they're signed out at once and can't sign in again, and their
  access to shared stations is removed. Nothing is deleted. If they own
  stations, those are transferred to you first. **Reactivate** lets them sign in
  again; share stations with them again from each station's **People** tab.

## Letting users add their own stations

On a user's page, **May add their own stations** lets someone who isn't an
administrator add stations, up to **How many** (1 unless you change it). The
limit counts every station they own, including any transferred to them. They
then find **Add a station** in the menu under their name and on the station
list, and become the owner of what they add.

Stations users add for themselves don't use the site's own Ambient Weather or
Weather Underground API keys, which belong to whoever runs the site: there's no
gap-filling from ambientweather.net and no **Neighbours** tab. Their consoles
upload directly, as usual.

## Your home page

Visitors, administrators and people without a station of their own see the
site's **Default station** (set by administrators in **Site settings**). A
user who owns a station lands on their own station instead (their oldest, if
they have several).

## Sharing a station

On a station's **Manage → People** tab, the owner (or an administrator):

- **adds someone** by their exact username, as a viewer, an advanced viewer or a
  manager. They need an account first;
- **changes** a person's role, or **removes** them.

Sharing works the same for public and private stations: on a public station,
a viewer additionally sees the indoor readings, private sensors and private log
entries.

## Transferring ownership

Also on the **People** tab, **Transfer ownership** hands the station to another
user. **Keep me on as a manager** (ticked by default) leaves the previous owner
with manager access; untick it and they lose access, unless the station is
public. Administrators choose the new owner from a list; owners type the
username. The transfer is recorded as a private entry in the
[station log](settings.md#station-log).

Administrators can also choose the owner when they **Add a station**.
