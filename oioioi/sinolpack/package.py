import glob
import logging
import re
import shutil
import tempfile
import os
import zipfile

from django.core.validators import slug_re
from django.utils.translation import ugettext as _
from django.core.files import File
import chardet

from oioioi.base.utils import naturalsort_key
from oioioi.base.utils.archive import Archive
from oioioi.base.utils.execute import execute, ExecuteError
from oioioi.problems.models import Problem, ProblemStatement
from oioioi.problems.package import ProblemPackageBackend, \
        ProblemPackageError
from oioioi.programs.models import Test, OutputChecker, ModelSolution
from oioioi.sinolpack.models import ExtraConfig, ExtraFile, OriginalPackage
from oioioi.filetracker.utils import stream_file


logger = logging.getLogger(__name__)

DEFAULT_TIME_LIMIT = 10000
DEFAULT_MEMORY_LIMIT = 66000


def _stringify_keys(dictionary):
    return dict((str(k), v) for k, v in dictionary.iteritems())


def _determine_encoding(title, file):
    r = re.search(r'\\documentclass\[(.+)\]{sinol}', file)
    encoding = 'latin2'

    if r is not None and 'utf8' in r.group(1):
        encoding = 'utf8'
    else:
        result = chardet.detect(title)
        if result['encoding'] == 'utf-8':
            encoding = 'utf-8'

    return encoding


def _decode(title, file):
    encoding = _determine_encoding(title, file)
    return title.decode(encoding)


class SinolPackage(object):
    def __init__(self, path, original_filename=None):
        self.filename = original_filename or path
        if self.filename.lower().endswith('.tar.gz'):
            ext = '.tar.gz'
        else:
            ext = os.path.splitext(self.filename)[1]
        self.archive = Archive(path, ext)
        self.config = None
        self.problem = None
        self.rootdir = None
        self.short_name = self._find_main_folder()

    def _find_main_folder(self):
        # Looks for the only folder which has at least the in/ and out/
        # subfolders.
        #
        # Note that depending on the archive type, there may be or
        # may not be entries for the folders themselves in
        # self.archive.filenames()

        files = map(os.path.normcase, self.archive.filenames())
        files = map(os.path.normpath, files)
        toplevel_folders = set(f.split(os.sep)[0] for f in files)
        toplevel_folders = filter(slug_re.match, toplevel_folders)
        problem_folders = []
        for folder in toplevel_folders:
            for required_subfolder in ('in', 'out'):
                if all(f.split(os.sep)[:2] != [folder, required_subfolder]
                       for f in files):
                    break
            else:
                problem_folders.append(folder)
        if len(problem_folders) == 1:
            return problem_folders[0]

    def identify(self):
        return self._find_main_folder() is not None

    def _process_config_yml(self):
        config_file = os.path.join(self.rootdir, 'config.yml')
        instance, created = \
                ExtraConfig.objects.get_or_create(problem=self.problem)
        if os.path.exists(config_file):
            instance.config = open(config_file, 'r').read()
        else:
            instance.config = ''
        instance.save()
        self.config = instance.parsed_config

    def _detect_full_name(self):
        """Sets the problem's full name from the ``config.yml`` (key ``title``)
           or from the ``title`` tag in the LateX source file.

           Example of how the ``title`` tag may look like:
           \title{A problem}
        """
        if 'title' in self.config:
           self.problem.name = self.config['title']
           self.problem.save()
           return

        source = os.path.join(self.rootdir, 'doc', self.short_name + 'zad.tex')
        if os.path.isfile(source):
            text = open(source, 'r').read()
            r = re.search(r'\\title{(.+)}', text)
            if r is not None:
                self.problem.name = _decode(r.group(1), text)
                self.problem.save()

    def _compile_docs(self, docdir):
        # fancyheadings.sty looks like a rarely available LaTeX package...
        src_fancyheadings = os.path.join(os.path.dirname(__file__), 'files',
                'fancyheadings.sty')
        dst_fancyheadings = os.path.join(docdir, 'fancyheadings.sty')
        if not os.path.exists(dst_fancyheadings):
            shutil.copyfile(src_fancyheadings, dst_fancyheadings)

        # Extract sinol.cls and oilogo.*, but do not overwrite if they
        # already exist (-k).
        sinol_cls_tgz = os.path.join(os.path.dirname(__file__), 'files',
                'sinol-cls.tgz')
        execute(['tar', '-C', docdir, '-kzxf', sinol_cls_tgz], cwd=docdir)

        try:
            execute('make', cwd=docdir)
        except ExecuteError:
            logger.warning('%s: failed to compile statement', self.filename,
                    exc_info=True)

    def _process_statements(self):
        docdir = os.path.join(self.rootdir, 'doc')
        if not os.path.isdir(docdir):
            logger.warning('%s: no docdir', self.filename)
            return

        self.problem.statements.all().delete()

        htmlzipfile = os.path.join(docdir, self.short_name + 'zad.html.zip')
        if os.path.exists(htmlzipfile):
            statement = ProblemStatement(problem=self.problem)
            statement.content.save(self.short_name + '.html.zip',
                    File(open(htmlzipfile, 'rb')))

        pdffile = os.path.join(docdir, self.short_name + 'zad.pdf')

        if not os.path.isfile(pdffile):
            self._compile_docs(docdir)
        if not os.path.isfile(pdffile):
            logger.warning('%s: no problem statement', self.filename)
            return

        statement = ProblemStatement(problem=self.problem)
        statement.content.save(self.short_name + '.pdf',
                File(open(pdffile, 'rb')))

    def _extract_makefiles(self):
        sinol_makefiles_tgz = os.path.join(os.path.dirname(__file__),
                'files', 'sinol-makefiles.tgz')
        Archive(sinol_makefiles_tgz).extract(to_path=self.rootdir)

        makefile_in = os.path.join(self.rootdir, 'makefile.in')
        if not os.path.exists(makefile_in):
           with open(makefile_in, 'w') as f:
                f.write('MODE=wer\n')
                f.write('ID=%s\n' % (self.short_name,))
                f.write('SIG=xxxx000\n')

    def _generate_tests(self):
        logger.info('%s: ingen', self.filename)
        execute('make ingen', cwd=self.rootdir)

        if glob.glob(os.path.join(self.rootdir, 'prog',
                '%sinwer.*' % (self.short_name,))):
            logger.info('%s: inwer', self.filename)
            execute('make inwer', cwd=self.rootdir)
        else:
            logger.info('%s: no inwer in package', self.filename)

        indir = os.path.join(self.rootdir, 'in')
        outdir = os.path.join(self.rootdir, 'out')
        for test in os.listdir(indir):
            basename = os.path.splitext(test)[0]
            if not os.path.exists(os.path.join(outdir, basename + '.out')):
                logger.info('%s: outgen', self.filename)
                execute('make outgen', cwd=self.rootdir)
                break

    def _process_tests(self, total_score=100):
        indir = os.path.join(self.rootdir, 'in')
        outdir = os.path.join(self.rootdir, 'out')
        test_names = []
        scored_groups = set()
        names_re = re.compile(r'^(%s(([0-9]+)([a-z]?[a-z0-9]*))).in$'
                % (re.escape(self.short_name),))

        time_limits = _stringify_keys(self.config.get('time_limits', {}))
        memory_limits = _stringify_keys(self.config.get('memory_limits', {}))

        # Find tests and create objects
        for order, test in enumerate(sorted(os.listdir(indir),
                                            key=naturalsort_key)):
            match = names_re.match(test)
            if not match:
                if test.endswith('.in'):
                    raise ProblemPackageError("Unrecognized test: " + test)
                continue

            # Examples for odl0ocen.in:
            basename = match.group(1)    # odl0ocen
            name = match.group(2)        # 0ocen
            group = match.group(3)       # 0
            suffix = match.group(4)      # ocen

            instance, created = Test.objects.get_or_create(
                problem=self.problem, name=name)
            instance.input_file.save(basename + '.in',
                    File(open(os.path.join(indir, basename + '.in'), 'rb')))
            instance.output_file.save(basename + '.out',
                    File(open(os.path.join(outdir, basename + '.out'), 'rb')))
            if group == '0' or 'ocen' in suffix:
                # Example tests
                instance.kind = 'EXAMPLE'
                instance.group = name
            else:
                instance.kind = 'NORMAL'
                instance.group = group
                scored_groups.add(group)

            if created:
                instance.time_limit = time_limits.get(name, DEFAULT_TIME_LIMIT)
                if 'memory_limit' in self.config:
                    instance.memory_limit = self.config['memory_limit']
                else:
                    instance.memory_limit = memory_limits.get(name,
                            DEFAULT_MEMORY_LIMIT)
            instance.order = order
            instance.save()
            test_names.append(name)

        # Delete nonexistent tests
        for test in Test.objects.filter(problem=self.problem) \
                .exclude(name__in=test_names):
            logger.info('%s: deleting test %s', self.filename, test.name)
            test.delete()

        # Assign scores
        if scored_groups:
            Test.objects.filter(problem=self.problem).update(max_score=0)
            num_groups = len(scored_groups)
            group_score = total_score / num_groups
            extra_score_groups = sorted(scored_groups, key=naturalsort_key)[
                    num_groups - (total_score - num_groups * group_score):]
            for group in scored_groups:
                score = group_score
                if group in extra_score_groups:
                    score += 1
                Test.objects.filter(problem=self.problem, group=group) \
                        .update(max_score=score)

    def _process_checkers(self):
        checker_prefix = os.path.join(self.rootdir, 'prog',
                self.short_name + 'chk')
        checker = None

        source_candidates = [
                checker_prefix + '.cpp',
                checker_prefix + '.c',
                checker_prefix + '.py',
                checker_prefix + '.pas',
            ]
        for source in source_candidates:
            if os.path.isfile(source):
                logger.info('%s: compiling checker', self.filename)
                execute(['make', self.short_name + 'chk.e'],
                        cwd=os.path.join(self.rootdir, 'prog'))
                break

        exe_candidates = [
                checker_prefix + '.e',
                checker_prefix + '.sh',
            ]
        for exe in exe_candidates:
            if os.path.isfile(exe):
                checker = exe

        instance = OutputChecker.objects.get(problem=self.problem)
        if checker:
            instance.exe_file.save(os.path.basename(checker),
                File(open(checker, 'rb')))
        else:
            instance.exe_file = None
            instance.save()

    def _process_extra_files(self):
        ExtraFile.objects.filter(problem=self.problem).delete()
        for filename in self.config.get('extra_compilation_files', ()):
            fn = os.path.join(self.rootdir, 'prog', filename)
            if not os.path.exists(fn):
                raise ProblemPackageError(_("Expected extra file '%s' not "
                    "found in prog/") % (filename,))
            instance = ExtraFile(problem=self.problem, name=filename)
            instance.file.save(filename, File(open(fn, 'rb')))

    def _process_model_solutions(self):
        ModelSolution.objects.filter(problem=self.problem).delete()

        names_re = re.compile(r'^%s[0-9]*([bs]?)[0-9]*\.(c|cpp|py|pas|java)'
                % (re.escape(self.short_name),))
        progdir = os.path.join(self.rootdir, 'prog')
        for name in os.listdir(progdir):
            path = os.path.join(progdir, name)
            if not os.path.isfile(path):
                continue
            match = names_re.match(name)
            if match:
                instance = ModelSolution(problem=self.problem, name=name)
                instance.kind = {
                        '': 'NORMAL',
                        's': 'SLOW',
                        'b': 'INCORRECT',
                    }[match.group(1)]
                instance.source_file.save(name, File(open(path, 'rb')))
                logger.info('%s: model solution: %s', self.filename, name)

    def _save_original_package(self):
        original_package, created = \
                OriginalPackage.objects.get_or_create(problem=self.problem)
        original_package.package_file.save(os.path.basename(self.filename),
                File(open(self.archive.filename, 'rb')))

    def unpack(self, existing_problem=None):
        self.short_name = self._find_main_folder()

        if existing_problem:
            self.problem = existing_problem
            if existing_problem.short_name != self.short_name:
                raise ProblemPackageError(_("Tried to replace problem "
                    "'%(oldname)s' with '%(newname)s'. For safety, changing "
                    "problem short name is not possible.") %
                    dict(oldname=existing_problem.short_name,
                        newname=self.short_name))
        else:
            self.problem = Problem(
                    name=self.short_name,
                    short_name=self.short_name,
                    controller_name='oioioi.sinolpack.controllers.SinolProblemController')

        self.problem.package_backend_name = \
                'oioioi.sinolpack.package.SinolPackageBackend'
        self.problem.save()

        tmpdir = tempfile.mkdtemp()
        logger.info('%s: tmpdir is %s', self.filename, tmpdir)
        try:
            self.archive.extract(to_path=tmpdir)
            self.rootdir = os.path.join(tmpdir, self.short_name)
            self._process_config_yml()
            self._detect_full_name()
            self._extract_makefiles()
            self._process_statements()
            self._generate_tests()
            self._process_tests()
            self._process_checkers()
            self._process_extra_files()
            self._process_model_solutions()
            self._save_original_package()
            return self.problem
        finally:
            shutil.rmtree(tmpdir)


class SinolPackageCreator(object):
    def __init__(self, problem):
        self.problem = problem
        self.short_name = problem.short_name
        self.zip = None

    def _pack_django_file(self, django_file, arcname):
        reader = django_file.file
        if hasattr(reader.file, 'name'):
            self.zip.write(reader.file.name, arcname)
        else:
            fd, name = tempfile.mkstemp()
            fileobj = os.fdopen(fd, 'wb')
            try:
                shutil.copyfileobj(reader, fileobj)
                fileobj.close()
                self.zip.write(name, arcname)
            finally:
                os.unlink(name)

    def _pack_statement(self):
        for statement in ProblemStatement.objects.filter(problem=self.problem):
            if not statement.content.name.endswith('.pdf'):
                continue
            if statement.language:
                filename = os.path.join(self.short_name, 'doc', '%szad-%s.pdf'
                        % (self.short_name, statement.language))
            else:
                filename = os.path.join(self.short_name, 'doc', '%szad.pdf'
                        % (self.short_name,))
            self._pack_django_file(statement.content, filename)

    def _pack_tests(self):
        for test in Test.objects.filter(problem=self.problem):
            basename = '%s%s' % (self.short_name, test.name)
            self._pack_django_file(test.input_file,
                    os.path.join(self.short_name, 'in', basename + '.in'))
            self._pack_django_file(test.output_file,
                    os.path.join(self.short_name, 'out', basename + '.out'))

    def _pack_model_solutions(self):
        for solution in ModelSolution.objects.filter(problem=self.problem):
            self._pack_django_file(solution.source_file,
                    os.path.join(self.short_name, 'prog', solution.name))

    def pack(self):
        try:
            original_package = OriginalPackage.objects.get(
                    problem=self.problem)
            return stream_file(original_package.package_file)
        except OriginalPackage.DoesNotExist:
            # If the original package is not available, produce the most basic
            # output: tests, statements, model solutions.
            fd, tmp_filename = tempfile.mkstemp()
            try:
                self.zip = zipfile.ZipFile(os.fdopen(fd, 'wb'), 'w')
                self._pack_statement()
                self._pack_tests()
                self._pack_model_solutions()
                self.zip.close()
                zip_filename = '%s.zip' % (self.short_name,)
                return stream_file(File(open(tmp_filename, 'rb'),
                    name=zip_filename))
            finally:
                os.unlink(tmp_filename)


class SinolPackageBackend(ProblemPackageBackend):
    description = _("Sinol Package")

    def identify(self, path, original_filename=None):
        print original_filename
        return SinolPackage(path, original_filename).identify()

    def unpack(self, path, original_filename=None, existing_problem=None):
        return SinolPackage(path, original_filename) \
                .unpack(existing_problem)

    def pack(self, problem):
        return SinolPackageCreator(problem).pack()
