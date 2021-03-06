import os.path
import itertools
import logging
from operator import attrgetter

from django.conf import settings
from django.core.exceptions import ValidationError
from django import forms
from django.template import RequestContext
from django.template.loader import render_to_string
from django.utils.translation import ugettext_lazy as _
from django.contrib.auth.models import User

from oioioi.problems.controllers import ProblemController
from oioioi.contests.controllers import ContestController
from oioioi.contests.models import SubmissionReport, ScoreReport
from oioioi.contests.controllers import submission_template_context
from oioioi.programs.models import ProgramSubmission, OutputChecker, \
        CompilationReport, TestReport, GroupReport, ModelProgramSubmission, \
        Submission
from oioioi.filetracker.utils import django_to_filetracker_path
from oioioi.evalmgr import recipe_placeholder, add_before_placeholder, \
        extend_after_placeholder
from oioioi.contests.utils import is_contest_admin

logger = logging.getLogger(__name__)


class ProgrammingProblemController(ProblemController):
    description = _("Simple programming problem")

    def generate_base_environ(self, environ, **kwargs):
        environ['recipe'] = [
                ('compile',
                    'oioioi.programs.handlers.compile'),

                recipe_placeholder('after_compile'),

                ('delete_executable',
                    'oioioi.programs.handlers.delete_executable'),
            ]
        environ['error_handlers'] = [('delete_executable',
                    'oioioi.programs.handlers.delete_executable')]

        if getattr(settings, 'USE_UNSAFE_EXEC', False):
            environ['exec_mode'] = 'unsafe'
        else:
            environ['exec_mode'] = settings.SAFE_EXEC_MODE

        if getattr(settings, 'USE_LOCAL_COMPILERS', False):
            environ['compiler'] = 'system-' + environ['language']

    def generate_recipe(self, kinds):
        recipe_body = [
                ('collect_tests',
                'oioioi.programs.handlers.collect_tests'),
        ]

        if 'INITIAL' in kinds:
            recipe_body.extend(
                    [
                        ('initial_run_tests',
                            'oioioi.programs.handlers.run_tests',
                            dict(kind='EXAMPLE')),
                        ('initial_grade_tests',
                            'oioioi.programs.handlers.grade_tests'),
                        ('initial_grade_groups',
                            'oioioi.programs.handlers.grade_groups'),
                        ('initial_grade_submission',
                            'oioioi.programs.handlers.grade_submission',
                            dict(kind='EXAMPLE')),
                        ('initial_make_report',
                            'oioioi.programs.handlers.make_report',
                            dict(kind='INITIAL')),
                        recipe_placeholder('after_initial_tests'),
                    ]
            )

        if 'NORMAL' in kinds or 'HIDDEN' in kinds:
            recipe_body.append(recipe_placeholder('before_final_tests'))

        if 'NORMAL' in kinds:
            recipe_body.extend(
                [
                    ('final_run_tests',
                        'oioioi.programs.handlers.run_tests',
                        dict(kind='NORMAL')),
                    ('final_grade_tests',
                        'oioioi.programs.handlers.grade_tests'),
                    ('final_grade_groups',
                        'oioioi.programs.handlers.grade_groups'),
                    ('final_grade_submission',
                        'oioioi.programs.handlers.grade_submission'),
                    ('final_make_report',
                        'oioioi.programs.handlers.make_report'),
                    recipe_placeholder('after_final_tests'),
                ])

        if 'HIDDEN' in kinds:
            recipe_body.extend(
                [
                    ('hidden_run_tests',
                        'oioioi.programs.handlers.run_tests'),
                    ('hidden_grade_tests',
                        'oioioi.programs.handlers.grade_tests'),
                    ('hidden_grade_groups',
                        'oioioi.programs.handlers.grade_groups'),
                    ('hidden_grade_submission',
                        'oioioi.programs.handlers.grade_submission',
                        dict(kind=None)),
                    ('hidden_make_report',
                        'oioioi.programs.handlers.make_report',
                        dict(kind='HIDDEN')),
                    recipe_placeholder('after_all_tests'),
                ])
        return recipe_body

    def fill_evaluation_environ(self, environ, **kwargs):
        self.generate_base_environ(environ, **kwargs)

        recipe_body = self.generate_recipe(environ['report_kinds'])

        extend_after_placeholder(environ, 'after_compile', recipe_body)

        environ.setdefault('group_scorer',
                            'oioioi.programs.utils.min_group_scorer')
        environ.setdefault('score_aggregator',
                'oioioi.programs.utils.sum_score_aggregator')

        checker = OutputChecker.objects.get(problem=self.problem).exe_file
        if checker:
            environ['checker'] = django_to_filetracker_path(checker)

    def mixins_for_admin(self):
        from oioioi.programs.admin import ProgrammingProblemAdminMixin
        return (ProgrammingProblemAdminMixin,)


class ProgrammingContestController(ContestController):
    description = _("Simple programming contest")

    def get_compilation_result_size_limit(self):
        return 10 * 1024 * 1024

    def _get_language(self, source_file):
        return os.path.splitext(source_file.name)[1][1:]

    def fill_evaluation_environ(self, environ, submission):
        submission = submission.programsubmission
        environ['source_file'] = \
            django_to_filetracker_path(submission.source_file)
        environ['language'] = self._get_language(submission.source_file)
        environ['compilation_result_size_limit'] = \
            self.get_compilation_result_size_limit()

        super(ProgrammingContestController,
                self).fill_evaluation_environ(environ, submission)

        self.fill_evaluation_environ_post_problem(environ, submission)

    def fill_evaluation_environ_post_problem(self, environ, submission):
        """Run after ProblemController.fill_evaluation_environ."""
        if 'INITIAL' in environ['report_kinds']:
            add_before_placeholder(environ, 'after_initial_tests',
                    ('update_report_statuses',
                        'oioioi.contests.handlers.update_report_statuses'))
            add_before_placeholder(environ, 'after_initial_tests',
                    ('update_submission_score',
                        'oioioi.contests.handlers.update_submission_score'))

    def get_submission_size_limit(self):
        return 102400  # in bytes

    def get_allowed_extensions(self):
        return getattr(settings, 'SUBMITTABLE_EXTENSIONS', [])

    def adjust_submission_form(self, request, form):
        size_limit = self.get_submission_size_limit()

        def validate_file_size(file):
            if file.size > size_limit:
                raise ValidationError(_("File size limit exceeded."))

        def validate_language(file):
            ext = self._get_language(file)
            if ext not in self.get_allowed_extensions():
                raise ValidationError(_(
                    "Unknown or not supported file extension."))

        form.fields['file'] = forms.FileField(allow_empty_file=False,
                validators=[validate_file_size, validate_language],
                label=_("File"),
                help_text=_("Language is determined by the file extension."
                            " It has to be one of: %s.") %
                            (', '.join(self.get_allowed_extensions()),)
                )

        if is_contest_admin(request):
            form.fields['user'] = forms.CharField(label=_("User"),
                    initial=request.user.username)

            def clean_user():
                username = form.cleaned_data['user']
                if username == request.user.username:
                    return request.user

                qs = User.objects.filter(username=username)
                try:
                    if request.user.is_superuser:
                        return qs.get()
                    else:
                        return self.registration_controller() \
                                .filter_participants(qs) \
                                .get()
                except User.DoesNotExist:
                    raise forms.ValidationError(_(
                            "User does not exist or "
                            "you do not have enough privileges"))
            form.clean_user = clean_user
            form.fields['kind'] = forms.ChoiceField(choices=[
                ('NORMAL', _("Normal")), ('IGNORED', _("Ignored"))],
                initial=form.kind, label=_("Kind"))

    def create_submission(self, request, problem_instance, form_data,
                    judge_after_create=True, **kwargs):
        submission = ProgramSubmission(
                user=form_data.get('user', request.user),
                problem_instance=problem_instance,
                kind=form_data.get('kind',
                        self.get_default_submission_kind(request)),
                date=request.timestamp)
        file = form_data['file']
        submission.source_file.save(file.name, file)
        submission.save()
        if judge_after_create:
            self.judge(submission)
        return submission

    def update_report_statuses(self, submission, queryset):
        self._activate_newest_report(submission, queryset,
                kind=['NORMAL', 'FAILURE'])
        self._activate_newest_report(submission, queryset,
                kind=['INITIAL'])

    def can_see_submission_status(self, request, submission):
        """Statuses are taken from initial tests which are always public."""
        return True

    def _map_report_to_submission_status(self, status):
        mapping = {'OK': 'INI_OK', 'CE': 'CE', 'SE': 'SE'}
        return mapping.get(status, 'INI_ERR')

    def update_submission_score(self, submission):
        # Status is taken from the initial report
        try:
            report = SubmissionReport.objects.filter(submission=submission,
                    status='ACTIVE', kind='INITIAL').get()
            score_report = ScoreReport.objects.get(submission_report=report)
            submission.status = self._map_report_to_submission_status(
                    score_report.status)
        except SubmissionReport.DoesNotExist:
            if SubmissionReport.objects.filter(submission=submission,
                    status='ACTIVE', kind='FAILURE'):
                submission.status = 'SE'
            else:
                submission.status = '?'

        # Score from the final
        try:
            report = SubmissionReport.objects.filter(submission=submission,
                    status='ACTIVE', kind='NORMAL').get()
            score_report = ScoreReport.objects.get(submission_report=report)
            submission.score = score_report.score
        except SubmissionReport.DoesNotExist:
            submission.score = None
        submission.save()

    def get_visible_reports_kinds(self, request, submission):
        if self.results_visible(request, submission):
            return ['INITIAL', 'NORMAL']
        else:
            return ['INITIAL']

    def filter_visible_reports(self, request, submission, queryset):
        if is_contest_admin(request):
            return queryset
        else:
            return queryset.filter(status='ACTIVE',
                    kind__in=self.get_visible_reports_kinds(request,
                                                            submission))

    def render_submission(self, request, submission):
        return render_to_string('programs/submission_header.html',
                context_instance=RequestContext(request,
                    {'submission': submission_template_context(request,
                        submission.programsubmission),
                    'saved_diff_id': request.session.get('saved_diff_id'),
                    'supported_extra_args':
                        self.get_supported_extra_args(submission)}))

    def render_report(self, request, report):
        if report.kind == 'FAILURE':
            return ContestController.render_report(self, request,
                    report)

        score_report = ScoreReport.objects.get(submission_report=report)
        compilation_report = \
                CompilationReport.objects.get(submission_report=report)
        test_reports = TestReport.objects.filter(submission_report=report) \
                .order_by('test__order', 'test_group', 'test_name')
        group_reports = GroupReport.objects.filter(submission_report=report)
        group_reports = dict((g.group, g) for g in group_reports)

        groups = []
        for group_name, tests in itertools.groupby(test_reports,
                attrgetter('test_group')):
            groups.append({'tests': list(tests),
                'report': group_reports[group_name]})

        return render_to_string('programs/report.html',
                context_instance=RequestContext(request,
                    {'report': report, 'score_report': score_report,
                        'compilation_report': compilation_report,
                        'groups': groups}))

    def render_submission_footer(self, request, submission):
        super_footer = super(ProgrammingContestController, self). \
                render_submission_footer(request, submission)
        queryset = Submission.objects \
                .filter(problem_instance__contest=request.contest) \
                .filter(user=submission.user) \
                .filter(problem_instance=submission.problem_instance) \
                .exclude(pk=submission.pk) \
                .order_by('-date') \
                .select_related()
        if not is_contest_admin(request):
            cc = request.contest.controller
            queryset = cc.filter_my_visible_submissions(request, queryset)
        show_scores = bool(queryset.filter(score__isnull=False))
        if not queryset.exists():
            return super_footer
        return super_footer + render_to_string(
                'programs/other_submissions.html',
                context_instance=RequestContext(request, {
                        'submissions': [submission_template_context(request, s)
                                 for s in queryset],
                        'show_scores': show_scores,
                        'main_submission_id': submission.id,
                        'submissions_on_page': getattr(settings,
                            'SUBMISSIONS_ON_PAGE', 15)}))

    def valid_kinds_for_submission(self, submission):
        if ModelProgramSubmission.objects.filter(id=submission.id).exists():
            return [submission.kind]

        return super(ProgrammingContestController, self) \
                .valid_kinds_for_submission(submission)
