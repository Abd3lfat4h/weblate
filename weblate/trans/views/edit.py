# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2017 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from __future__ import unicode_literals

import copy
import uuid
import time

from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST
from django.utils.translation import ugettext as _, ungettext
from django.utils.encoding import force_text
from django.http import HttpResponseRedirect, HttpResponse
from django.contrib.auth.decorators import login_required
from django.utils import formats
from django.core.exceptions import PermissionDenied

from weblate.utils import messages
from weblate.permissions.helpers import check_access
from weblate.trans.models import (
    Unit, Change, Comment, Suggestion, Dictionary,
    get_related_units,
)
from weblate.trans.autofixes import fix_target
from weblate.trans.forms import (
    TranslationForm, SearchForm, InlineWordForm,
    MergeForm, AutoForm, ReviewForm, ReplaceForm,
    AntispamForm, CommentForm, RevertForm
)
from weblate.trans.views.helper import (
    get_translation, get_subproject, get_project, import_message,
    show_form_errors,
)
from weblate.trans.checks import CHECKS
from weblate.trans.util import join_plural, render
from weblate.trans.autotranslate import auto_translate
from weblate.permissions.helpers import (
    can_translate, can_suggest, can_accept_suggestion, can_delete_suggestion,
    can_vote_suggestion, can_delete_comment, can_automatic_translation,
    can_add_comment,
)
from weblate.utils.hash import checksum_to_hash


def cleanup_session(session):
    """Delete old search results from session storage."""
    now = int(time.time())
    keys = list(session.keys())
    for key in keys:
        if not key.startswith('search_'):
            continue
        value = session[key]
        if not isinstance(value, dict) or value['ttl'] < now:
            del session[key]


def search(translation, request):
    """Perform search or returns cached search results."""

    # Already performed search
    if 'sid' in request.GET:
        # Grab from session storage
        search_id = 'search_{0}'.format(request.GET['sid'])

        # Check if we know the search
        if search_id not in request.session:
            messages.error(request, _('Invalid search string!'))
            return redirect(translation)

        search_result = copy.copy(request.session[search_id])
        if 'params' in search_result:
            search_result['form'] = SearchForm(search_result['params'])
        else:
            search_result['form'] = SearchForm()

        return search_result

    # Possible new search
    search_form = SearchForm(request.GET)
    review_form = ReviewForm(request.GET)

    search_query = None
    if 'date' in request.GET:
        if review_form.is_valid():
            # Review
            allunits = translation.unit_set.review(
                review_form.cleaned_data['date'],
                request.user
            )

            formatted_date = formats.date_format(
                review_form.cleaned_data['date'],
                'SHORT_DATE_FORMAT'
            )
            name = _('Review of translations since %s') % formatted_date
        else:
            show_form_errors(request, review_form)

            # Filtering by type
            allunits = translation.unit_set.all()
            name = _('All strings')
    elif search_form.is_valid():
        # Apply search conditions
        allunits = translation.unit_set.search(
            translation,
            search_form.cleaned_data,
        )
        if search_form.cleaned_data['type'] == 'random':
            allunits = allunits[:25]

        search_query = search_form.cleaned_data['q']
        name = search_form.get_name()
    else:
        # Error reporting
        show_form_errors(request, search_form)

        # Filtering by type
        allunits = translation.unit_set.all()
        name = _('All strings')

    # Grab unit IDs
    unit_ids = list(allunits.values_list('id', flat=True))

    # Check empty search results
    if len(unit_ids) == 0:
        messages.warning(request, _('No string matched your search!'))
        return redirect(translation)

    # Checksum unit access
    offset = 0
    if 'checksum' in request.GET:
        try:
            unit = allunits.filter(
                id_hash=checksum_to_hash(request.GET['checksum'])
            )[0]
            offset = unit_ids.index(unit.id)
        except (Unit.DoesNotExist, IndexError, ValueError):
            messages.warning(request, _('No string matched your search!'))
            return redirect(translation)

    # Remove old search results
    cleanup_session(request.session)

    # Store in cache and return
    search_id = str(uuid.uuid1())
    search_result = {
        'params': request.GET,
        'query': search_query,
        'name': force_text(name),
        'ids': unit_ids,
        'search_id': search_id,
        'ttl': int(time.time()) + 86400,
        'offset': offset,
    }

    request.session['search_{0}'.format(search_id)] = search_result

    search_result = copy.copy(search_result)
    search_result['form'] = search_form
    return search_result


def perform_suggestion(unit, form, request):
    """Handle suggesion saving."""
    if form.cleaned_data['target'][0] == '':
        messages.error(request, _('Your suggestion is empty!'))
        # Stay on same entry
        return False
    elif not can_suggest(request.user, unit.translation):
        # Need privilege to add
        messages.error(
            request,
            _('You don\'t have privileges to add suggestions!')
        )
        # Stay on same entry
        return False
    # Invite user to become translator if there is nobody else
    recent_changes = Change.objects.content(True).filter(
        translation=unit.translation,
    ).exclude(
        user=None
    )
    if not recent_changes.exists():
        messages.info(request, _(
            'There is currently no active translator for this '
            'translation, please consider becoming a translator '
            'as your suggestion might otherwise remain unreviewed.'
        ))
    # Create the suggestion
    result = Suggestion.objects.add(
        unit,
        join_plural(form.cleaned_data['target']),
        request,
        can_vote_suggestion(request.user, unit.translation)
    )
    if not result:
        messages.error(request, _('Your suggestion already exists!'))
    return result


def perform_translation(unit, form, request):
    """Handle translation and stores it to a backend."""
    # Remember old checks
    oldchecks = set(
        unit.active_checks().values_list('check', flat=True)
    )

    # Run AutoFixes on user input
    if not unit.translation.is_template():
        new_target, fixups = fix_target(form.cleaned_data['target'], unit)
    else:
        new_target = form.cleaned_data['target']
        fixups = []

    # Save
    saved = unit.translate(
        request,
        new_target,
        form.cleaned_data['fuzzy']
    )

    # Warn about applied fixups
    if len(fixups) > 0:
        messages.info(
            request,
            _('Following fixups were applied to translation: %s') %
            ', '.join([force_text(f) for f in fixups])
        )

    # Get new set of checks
    newchecks = set(
        unit.active_checks().values_list('check', flat=True)
    )

    # Did we introduce any new failures?
    if saved and newchecks > oldchecks:
        # Show message to user
        messages.error(
            request,
            _(
                'Some checks have failed on your translation: {0}'
            ).format(
                ', '.join(
                    [force_text(CHECKS[check].name) for check in newchecks]
                )
            )
        )
        # Stay on same entry
        return False

    return True


def handle_translate(translation, request, user_locked,
                     this_unit_url, next_unit_url):
    """Save translation or suggestion to database and backend."""
    # Antispam protection
    antispam = AntispamForm(request.POST)
    if not antispam.is_valid():
        # Silently redirect to next entry
        return HttpResponseRedirect(next_unit_url)

    # Check whether translation is not outdated
    translation.check_sync()

    form = TranslationForm(
        request.user.profile, translation, None, request.POST
    )
    if not form.is_valid():
        return

    unit = form.cleaned_data['unit']
    go_next = True

    if 'suggest' in request.POST:
        go_next = perform_suggestion(unit, form, request)
    elif not can_translate(request.user, unit.translation):
        messages.error(
            request,
            _('You don\'t have privileges to save translations!')
        )
    elif not user_locked:
        # Custom commit message
        message = request.POST.get('commit_message')
        if message is not None and message != unit.translation.commit_message:
            # Commit pending changes so that they don't get new message
            unit.translation.commit_pending(request, request.user)
            # Store new commit message
            unit.translation.commit_message = message
            unit.translation.save()

        go_next = perform_translation(unit, form, request)

    # Redirect to next entry
    if go_next:
        return HttpResponseRedirect(next_unit_url)
    else:
        return HttpResponseRedirect(this_unit_url)


def handle_merge(translation, request, next_unit_url):
    """Handle unit merging."""
    if not can_translate(request.user, translation):
        messages.error(
            request,
            _('You don\'t have privileges to save translations!')
        )
        return

    mergeform = MergeForm(translation, request.GET)
    if not mergeform.is_valid():
        messages.error(
            request,
            _('Invalid merge request!')
        )
        return

    unit = mergeform.cleaned_data['unit']

    merged = Unit.objects.get(
        pk=mergeform.cleaned_data['merge']
    )

    if unit.id_hash != merged.id_hash:
        messages.error(
            request,
            _('Can not merge different messages!')
        )
    else:
        # Store unit
        unit.target = merged.target
        unit.fuzzy = merged.fuzzy
        saved = unit.save_backend(request)
        # Update stats if there was change
        if saved:
            request.user.profile.translated += 1
            request.user.profile.save()
        # Redirect to next entry
        return HttpResponseRedirect(next_unit_url)


def handle_revert(translation, request, next_unit_url):
    if not can_translate(request.user, translation):
        messages.error(
            request,
            _('You don\'t have privileges to save translations!')
        )
        return

    revertform = RevertForm(translation, request.GET)
    if not revertform.is_valid():
        return

    unit = revertform.cleaned_data['unit']

    change = Change.objects.get(
        pk=revertform.cleaned_data['revert']
    )

    if unit.id_hash != change.unit.id_hash:
        messages.error(
            request,
            _('Can not revert to different unit!')
        )
    elif change.target == "":
        messages.error(
            request,
            _('Can not revert to empty translation!')
        )
    else:
        # Store unit
        unit.target = change.target
        unit.save_backend(request, change_action=Change.ACTION_REVERT)
        # Redirect to next entry
        return HttpResponseRedirect(next_unit_url)


def check_suggest_permissions(request, mode, translation, suggestion):
    """Check permission for suggestion handling."""
    if mode in ('accept', 'accept_edit'):
        if not can_accept_suggestion(request.user, translation):
            messages.error(
                request,
                _('You do not have privilege to accept suggestions!')
            )
            return False
    elif mode == 'delete':
        if not can_delete_suggestion(request.user, translation, suggestion):
            messages.error(
                request,
                _('You do not have privilege to delete suggestions!')
            )
            return False
    elif mode in ('upvote', 'downvote'):
        if not can_vote_suggestion(request.user, translation):
            messages.error(
                request,
                _('You do not have privilege to vote for suggestions!')
            )
            return False
    return True


def handle_suggestions(translation, request, this_unit_url, next_unit_url):
    """Handle suggestion deleting/accepting."""
    sugid = ''
    params = ('accept', 'accept_edit', 'delete', 'upvote', 'downvote')
    redirect_url = this_unit_url
    mode = None

    # Parse suggestion ID
    for param in params:
        if param in request.POST:
            sugid = request.POST[param]
            mode = param
            break

    # Fetch suggestion
    try:
        suggestion = Suggestion.objects.get(
            pk=int(sugid),
            project=translation.subproject.project,
            language=translation.language
        )
    except (Suggestion.DoesNotExist, ValueError):
        messages.error(request, _('Invalid suggestion!'))
        return HttpResponseRedirect(this_unit_url)

    # Permissions check
    if not check_suggest_permissions(request, mode, translation, suggestion):
        return HttpResponseRedirect(this_unit_url)

    # Perform operation
    if 'accept' in request.POST or 'accept_edit' in request.POST:
        suggestion.accept(translation, request)
        if 'accept' in request.POST:
            redirect_url = next_unit_url
    elif 'delete' in request.POST:
        suggestion.delete_log(translation, request)
    elif 'upvote' in request.POST:
        suggestion.add_vote(translation, request, True)
    elif 'downvote' in request.POST:
        suggestion.add_vote(translation, request, False)

    return HttpResponseRedirect(redirect_url)


def translate(request, project, subproject, lang):
    """Generic entry point for translating, suggesting and searching."""
    translation = get_translation(request, project, subproject, lang)

    # Check locks
    user_locked = translation.is_user_locked(request.user)
    project_locked = translation.subproject.locked
    locked = project_locked or user_locked

    # Search results
    search_result = search(translation, request)

    # Handle redirects
    if isinstance(search_result, HttpResponse):
        return search_result

    # Get numer of results
    num_results = len(search_result['ids'])

    # Search offset
    try:
        offset = int(request.GET.get('offset', search_result.get('offset', 0)))
    except ValueError:
        offset = 0

    # Check boundaries
    if not 0 <= offset < num_results:
        messages.info(request, _('You have reached end of translating.'))
        # Delete search
        del request.session['search_{0}'.format(search_result['search_id'])]
        # Redirect to translation
        return redirect(translation)

    # Some URLs we will most likely use
    base_unit_url = '{0}?sid={1}&offset='.format(
        translation.get_translate_url(),
        search_result['search_id']
    )
    this_unit_url = base_unit_url + str(offset)
    next_unit_url = base_unit_url + str(offset + 1)

    response = None

    # Any form submitted?
    if request.method == 'POST' and not project_locked:

        # Handle accepting/deleting suggestions
        if ('accept' not in request.POST and
                'accept_edit' not in request.POST and
                'delete' not in request.POST and
                'upvote' not in request.POST and
                'downvote' not in request.POST):
            response = handle_translate(
                translation, request, user_locked,
                this_unit_url, next_unit_url
            )
        elif not locked:
            response = handle_suggestions(
                translation, request, this_unit_url, next_unit_url,
            )

    # Handle translation merging
    elif 'merge' in request.GET and not locked:
        response = handle_merge(
            translation, request, next_unit_url
        )

    # Handle reverting
    elif 'revert' in request.GET and not locked:
        response = handle_revert(
            translation, request, this_unit_url
        )

    # Pass possible redirect further
    if response is not None:
        return response

    # Grab actual unit
    try:
        unit = translation.unit_set.get(pk=search_result['ids'][offset])
    except Unit.DoesNotExist:
        # Can happen when using SID for other translation
        messages.error(request, _('Invalid search string!'))
        return redirect(translation)

    # Show secondary languages for logged in users
    if request.user.is_authenticated:
        secondary = unit.get_secondary_units(request.user)
    else:
        secondary = None

    # Spam protection
    antispam = AntispamForm()

    # Prepare form
    form = TranslationForm(request.user.profile, translation, unit)

    others = Unit.objects.same(unit, False)
    # Is it only this unit?
    if others.count() == 1:
        others = Unit.objects.none()

    return render(
        request,
        'translate.html',
        {
            'this_unit_url': this_unit_url,
            'first_unit_url': base_unit_url + '0',
            'last_unit_url': base_unit_url + str(num_results - 1),
            'next_unit_url': next_unit_url,
            'prev_unit_url': base_unit_url + str(offset - 1),
            'object': translation,
            'project': translation.subproject.project,
            'unit': unit,
            'others': others,
            'others_count': others.exclude(target=unit.target).count(),
            'total': translation.unit_set.all().count(),
            'search_id': search_result['search_id'],
            'search_query': search_result['query'],
            'offset': offset,
            'filter_name': search_result['name'],
            'filter_count': num_results,
            'filter_pos': offset + 1,
            'form': form,
            'antispam': antispam,
            'comment_form': CommentForm(),
            'search_form': search_result['form'],
            'update_lock': translation.lock_user == request.user,
            'secondary': secondary,
            'locked': locked,
            'user_locked': user_locked,
            'project_locked': project_locked,
            'glossary': Dictionary.objects.get_words(unit),
            'addword_form': InlineWordForm(),
        }
    )


@require_POST
@login_required
def auto_translation(request, project, subproject, lang):
    translation = get_translation(request, project, subproject, lang)
    project = translation.subproject.project
    if not can_automatic_translation(request.user, project):
        raise PermissionDenied()

    autoform = AutoForm(translation, request.user, request.POST)

    if translation.subproject.locked or not autoform.is_valid():
        messages.error(request, _('Failed to process form!'))
        return redirect(translation)

    updated = auto_translate(
        request.user,
        translation,
        autoform.cleaned_data['subproject'],
        autoform.cleaned_data['inconsistent'],
        autoform.cleaned_data['overwrite']
    )

    import_message(
        request, updated,
        _('Automatic translation completed, no strings were updated.'),
        ungettext(
            'Automatic translation completed, %d string was updated.',
            'Automatic translation completed, %d strings were updated.',
            updated
        )
    )

    return redirect(translation)


@login_required
def comment(request, pk):
    """Add new comment."""
    unit = get_object_or_404(Unit, pk=pk)
    check_access(request, unit.translation.subproject.project)

    if not can_add_comment(request.user, unit.translation.subproject.project):
        raise PermissionDenied()

    form = CommentForm(request.POST)

    if form.is_valid():
        if form.cleaned_data['scope'] == 'global':
            lang = None
        else:
            lang = unit.translation.language
        Comment.objects.add(
            unit,
            request.user,
            lang,
            form.cleaned_data['comment']
        )
        messages.success(request, _('Posted new comment'))
    else:
        messages.error(request, _('Failed to add comment!'))

    return redirect(request.POST.get('next', unit))


@login_required
@require_POST
def delete_comment(request, pk):
    """Delete comment."""
    comment_obj = get_object_or_404(Comment, pk=pk)
    check_access(request, comment_obj.project)

    if not can_delete_comment(request.user, comment_obj.project):
        raise PermissionDenied()

    units = get_related_units(comment_obj)
    if units.exists():
        fallback_url = units[0].get_absolute_url()
    else:
        fallback_url = comment_obj.project.get_absolute_url()

    comment_obj.delete()
    messages.info(request, _('Translation comment has been deleted.'))

    return redirect(request.POST.get('next', fallback_url))


def get_zen_unitdata(translation, request):
    """Load unit data for zen mode."""
    # Search results
    search_result = search(translation, request)

    # Search offset
    try:
        offset = int(request.GET.get('offset', 0))
    except ValueError:
        offset = 0

    # Handle redirects
    if isinstance(search_result, HttpResponse):
        return search_result, None

    search_result['last_section'] = offset + 20 >= len(search_result['ids'])
    search_result['offset'] = offset

    units = translation.unit_set.filter(
        pk__in=search_result['ids'][offset:offset + 20]
    )

    unitdata = [
        {
            'unit': unit,
            'secondary': (
                unit.get_secondary_units(request.user)
                if request.user.is_authenticated and
                request.user.profile.secondary_in_zen
                else None
            ),
            'form': TranslationForm(
                request.user.profile,
                translation,
                unit,
                tabindex=100 + (unit.position * 10),
            ),
            'offset': offset + pos,
        }
        for pos, unit in enumerate(units)
    ]

    return search_result, unitdata


def zen(request, project, subproject, lang):
    """Generic entry point for translating, suggesting and searching."""
    translation = get_translation(request, project, subproject, lang)
    search_result, unitdata = get_zen_unitdata(translation, request)

    # Handle redirects
    if isinstance(search_result, HttpResponse):
        return search_result

    return render(
        request,
        'zen.html',
        {
            'object': translation,
            'project': translation.subproject.project,
            'unitdata': unitdata,
            'search_query': search_result['query'],
            'filter_name': search_result['name'],
            'filter_count': len(search_result['ids']),
            'last_section': search_result['last_section'],
            'search_id': search_result['search_id'],
            'offset': search_result['offset'],
            'search_form': search_result['form'],
            'update_lock': translation.lock_user == request.user,
        }
    )


def load_zen(request, project, subproject, lang):
    """Load additional units for zen editor."""
    translation = get_translation(request, project, subproject, lang)
    search_result, unitdata = get_zen_unitdata(translation, request)

    # Handle redirects
    if isinstance(search_result, HttpResponse):
        return search_result

    return render(
        request,
        'zen-units.html',
        {
            'object': translation,
            'unitdata': unitdata,
            'search_query': search_result['query'],
            'search_id': search_result['search_id'],
            'last_section': search_result['last_section'],
        }
    )


@login_required
@require_POST
def save_zen(request, project, subproject, lang):
    """Save handler for zen mode."""
    translation = get_translation(request, project, subproject, lang)
    user_locked = translation.is_user_locked(request.user)

    form = TranslationForm(
        request.user.profile, translation, None, request.POST
    )
    if not can_translate(request.user, translation):
        messages.error(
            request,
            _('You don\'t have privileges to save translations!')
        )
    elif not form.is_valid():
        messages.error(request, _('Failed to save translation!'))
    elif not user_locked:
        unit = form.cleaned_data['unit']

        perform_translation(unit, form, request)

    return render(
        request,
        'zen-response.html',
        {},
    )


@login_required
@require_POST
def search_replace(request, project, subproject=None, lang=None):
    if subproject is None:
        obj = get_project(request, project)
        perms = {'project': obj}
        unit_set = Unit.objects.filter(translation__subproject__project=obj)
    elif lang is None:
        obj = get_subproject(request, project, subproject)
        perms = {'project': obj.project}
        unit_set = Unit.objects.filter(translation__subproject=obj)
    else:
        obj = get_translation(request, project, subproject, lang)
        perms = {'translation': obj}
        unit_set = obj.unit_set

    if not can_translate(request.user, **perms):
        raise PermissionDenied()

    form = ReplaceForm(request.POST)

    if not form.is_valid():
        messages.error(request, _('Failed to process form!'))
        return redirect(obj)

    search_text = form.cleaned_data['search']
    replacement = form.cleaned_data['replacement']

    matching = unit_set.filter(target__contains=search_text)
    updated = matching.count()

    for unit in matching.iterator():
        unit.target = unit.target.replace(search_text, replacement)
        unit.save_backend(request, change_action=Change.ACTION_REPLACE)

    import_message(
        request, updated,
        _('Search and replace completed, no strings were updated.'),
        ungettext(
            'Search and replace completed, %d string was updated.',
            'Search and replace completed, %d strings were updated.',
            updated
        )
    )

    return redirect(obj)
