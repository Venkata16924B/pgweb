from django.shortcuts import render, get_object_or_404
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect, Http404
from django.template.loader import get_template
from django.db import models
import django.utils.xmlutils
from django.conf import settings

from pgweb.util.contexts import render_pgweb

import io
import difflib

from pgweb.mailqueue.util import send_simple_mail


def simple_form(instancetype, itemid, request, formclass, formtemplate='base/form.html', redirect='/account/', navsection='account', fixedfields=None, createifempty=False):
    if itemid == 'new':
        instance = instancetype()
        is_new = True
    else:
        is_new = False
        # Regular form item, attempt to edit it
        try:
            int(itemid)
        except ValueError:
            raise Http404("Invalid URL")
        if createifempty:
            (instance, wascreated) = instancetype.objects.get_or_create(pk=itemid)
        else:
            instance = get_object_or_404(instancetype, pk=itemid)
        if hasattr(instance, 'submitter'):
            if not instance.submitter == request.user:
                raise PermissionDenied("You are not the owner of this item!")
        elif hasattr(instance, 'verify_submitter'):
            if not instance.verify_submitter(request.user):
                raise PermissionDenied("You are not the owner of this item!")

    if request.method == 'POST':
        # Process this form
        form = formclass(data=request.POST, instance=instance)

        # Save away the old value from the instance before it's saved
        if not is_new:
            old_values = {fn: str(getattr(instance, fn)) for fn in form.changed_data if hasattr(instance, fn)}

        if form.is_valid():
            # We are handling notifications, so disable the ones we'd otherwise send
            do_notify = getattr(instance, 'send_notification', False)
            instance.send_notification = False

            if not getattr(instance, 'approved', True) and not is_new:
                # If the object has an "approved" field and it's set to false, we don't
                # bother notifying about the changes. But if it lacks this field, we notify
                # about everything, as well as if the field exists and the item has already
                # been approved.
                # Newly added objects are always notified.
                do_notify = False

            notify = io.StringIO()

            r = form.save(commit=False)
            r.submitter = request.user
            # Set fixed fields. Note that this will not work if the fixed fields are ManyToMany,
            # but we'll fix that sometime in the future
            if fixedfields:
                for k, v in list(fixedfields.items()):
                    setattr(r, k, v)
            r.save()

            # If we have a callback with the current user
            if hasattr(form, 'apply_submitter'):
                form.apply_submitter(r, request.user)
                r.save()

            if is_new:
                subj = 'A new {0} has been added'.format(instance._meta.verbose_name)
                for f in form.fields:
                    notify.write("{}:\n".format(f))
                    if instance._meta.get_field(f) in instance._meta.many_to_many:
                        notify.write("{}\n".format("\n".join([str(x) for x in form.cleaned_data[f]])))
                    else:
                        notify.write("{}\n".format(str(form.cleaned_data[f])))
                    notify.write("\n")
            else:
                subj = '{0} id {1} has been modified'.format(instance._meta.verbose_name, instance.id)

                for fn in form.changed_data:
                    if not hasattr(instance, fn):
                        continue
                    f = instance._meta.get_field(fn)
                    if f in instance._meta.many_to_many:
                        # m2m field have separate config of notificatgions
                        if getattr(instance, 'send_m2m_notification', False):
                            for f in instance._meta.many_to_many:
                                if f.name in form.cleaned_data:
                                    old = set([str(x) for x in getattr(instance, f.name).all()])
                                    new = set([str(x) for x in form.cleaned_data[f.name]])
                                    added = new.difference(old)
                                    removed = old.difference(new)
                                    if added or removed:
                                        notify.write("--- {}\n+++ {}\n{}\n{}\n".format(
                                            f.verbose_name,
                                            f.verbose_name,
                                            "\n".join(["+ %s" % a for a in added]),
                                            "\n".join(["- %s" % r for r in removed]),
                                        ))
                    else:
                        # Regular field!
                        # Sometimes it shows up as changed even if it hasn't changed, so do
                        # a second check on if the diff is non-empty.
                        diffrows = [x for x in
                                    difflib.unified_diff(
                                        old_values[f.name].splitlines(),
                                        str(form.cleaned_data[f.name]).splitlines(),
                                        n=1,
                                        lineterm='',
                                        fromfile=f.verbose_name,
                                        tofile=f.verbose_name,
                                    ) if not x.startswith("@@")]
                        if diffrows:
                            notify.write("\n".join(diffrows))
                            notify.write("\n\n")

            if do_notify and notify.tell():
                send_simple_mail(
                    settings.NOTIFICATION_FROM,
                    settings.NOTIFICATION_EMAIL,
                    "%s by %s" % (subj, request.user.username),
                    "Title: {0}\n\n{1}".format(
                        str(instance),
                        notify.getvalue(),
                    ),
                )
            form.save_m2m()

            return HttpResponseRedirect(redirect)
    else:
        # Generate form
        form = formclass(instance=instance)

    if hasattr(form, 'filter_by_user'):
        form.filter_by_user(request.user)

    for fn in form.fields:
        if fn in getattr(instancetype, 'markdown_fields', []):
            form.fields[fn].widget.attrs.update({'class': 'markdown-content'})

    for togg in getattr(form, 'toggle_fields', []):
        form.fields[togg['name']].widget.attrs.update({
            'data-toggles': ','.join(togg['fields']),
            'data-toggle-invert': togg['invert'] and 'true' or 'false',
            'class': 'toggle-checkbox',
        })

    return render_pgweb(request, navsection, formtemplate, {
        'form': form,
        'formitemtype': instance._meta.verbose_name,
        'form_intro': hasattr(form, 'form_intro') and form.form_intro or None,
        'described_checkboxes': getattr(form, 'described_checkboxes', {}),
        'savebutton': (itemid == "new") and "Submit New" or "Save",
        'operation': (itemid == "new") and "New" or "Edit",
    })


def template_to_string(templatename, attrs={}):
    return get_template(templatename).render(attrs)


def HttpServerError(request, msg):
    r = render(request, 'errors/500.html', {
        'message': msg,
    })
    r.status_code = 500
    return r


class PgXmlHelper(django.utils.xmlutils.SimplerXMLGenerator):
    def __init__(self, outstream, skipempty=False):
        django.utils.xmlutils.SimplerXMLGenerator.__init__(self, outstream, 'utf-8')
        self.skipempty = skipempty

    def add_xml_element(self, name, value):
        if self.skipempty and value == '':
            return
        self.startElement(name, {})
        self.characters(value)
        self.endElement(name)
