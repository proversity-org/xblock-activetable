# -*- coding: utf-8 -*-
"""An XBlock with a tabular problem type that requires students to fill in some cells."""
from __future__ import absolute_import, division, unicode_literals

import io
import re
import textwrap

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle
from submissions import api
from webob.response import Response
from xblock.core import XBlock
from xblock.fields import Dict, Float, Integer, Scope, String, Boolean, List
from xblock.fragment import Fragment
from xblock.validation import ValidationMessage
from xblockutils.resources import ResourceLoader
from xblockutils.studio_editable import StudioEditableXBlockMixin

from .cells import NumericCell, StaticCell, TextCell
from .parsers import ParseError, parse_table, parse_number_list, parse_to_pdf, parse_headers_to_list

loader = ResourceLoader(__name__)  # pylint: disable=invalid-name
SCORE_OPTIONS = ["scorable", "no_right_answer", "unique_option"]
ITEM_TYPE = "activetable"


class ActiveTableXBlock(StudioEditableXBlockMixin, XBlock):
    """An XBlock with a tabular problem type that requires students to fill in some cells."""

    display_name = String(
        display_name='Display Name',
        help='The title Studio uses for the component.',
        scope=Scope.settings,
        default='ActiveTable problem'
    )
    content = String(
        display_name='Table definition',
        help='The definition of the table in Python-like syntax.  Note that changing the table '
        'definition of a live problem will invalidate all student answers.',
        scope=Scope.content,
        multiline_editor=True,
        resettable_editor=False,
        default=textwrap.dedent("""\
        [
            ['Column header 1', 'Column header 2'],
            ['Enter "answer" here:', Text(answer='answer')],
            [42, Numeric(answer=42, tolerance=0.0)],
        ]
        """)
    )
    help_text = String(
        display_name='Help text',
        help='The text that gets displayed when clicking the "+help" button.  If you remove the '
        'help text, the help feature is disabled.',
        scope=Scope.content,
        multiline_editor=True,
        resettable_editor=False,
        default='Fill in the cells highlighted in yellow with the correct answers.  '
        'When you are done, you can check your answers using the button below the table.',
    )
    column_widths = String(
        display_name='Column widths',
        help='Set the width of the columns in pixels.  The value should be a Python-like list of '
        'numerical values.  The total width of the table should not be more than 800. Omitting '
        'this value will result in equal-width columns with a total width of 800 pixels.',
        scope=Scope.content,
        resettable_editor=False,
    )
    row_heights = String(
        display_name='Row heights',
        help='Set the heights of the rows in pixels.  The value should be a Python-like list of '
        'numerical values. Rows may grow higher than the specified value if the text in some cells '
        'in the row is long enough to get wrapped in more than one line.',
        scope=Scope.content,
        resettable_editor=False,
    )
    default_tolerance = Float(
        display_name='Default tolerance',
        help='The tolerance in percent that is used for numerical response cells you did not '
        'specify an explicit tolerance for.',
        scope=Scope.content,
        default=1.0,
    )
    maximum_score = Float(
        display_name='Maximum score',
        help='The number of points students will be awarded when solving all fields correctly.  '
        'For partially correct attempts, the score will be pro-rated.',
        scope=Scope.settings,
        default=1.0,
    )
    max_attempts = Integer(
        display_name='Maximum attempts',
        help='Defines the number of times a student can try to answer this problem.  If the value '
        'is not set, infinite attempts are allowed.',
        scope=Scope.settings,
    )

    score_type = String(
        display_name="Score type",
        help="Select if the answers will be checked  for correctness or if there will be a unique option",
        scope=Scope.settings,
        default=SCORE_OPTIONS[0],
        values_provider=lambda _: SCORE_OPTIONS,
    )

    extendable = Boolean(
        display_name="Extendable",
        help="If it's selected means the student can add additional rows at the end of the table.",
        scope=Scope.settings,
        default=False
    )

    custom_headers = Boolean(
        display_name="Custom Headers",
        help="Make true if you want to modify the headers style",
        scope=Scope.settings,
        default=False
    )

    headers_style = String(
        display_name='Headers Style',
        help='Include the new headers between the label <th></th> and inside the <thead> label.',
        scope=Scope.content,
        multiline_editor='html',
        resettable_editor=False,
        default=textwrap.dedent("""
            <table>
                <thead>
                <tr>
                <th>Header 1</th>
                <th>Header 2</th>
                <th>Header 3</th>
                </tr>
                </thead>
            </table>
        """)
    )

    editable_fields = [
        'display_name',
        'content',
        'custom_headers',
        'headers_style',
        'help_text',
        'column_widths',
        'row_heights',
        'default_tolerance',
        'maximum_score',
        'max_attempts',
        'score_type',
        'extendable',
    ]

    # Dictionary mapping cell ids to the student answers.
    answers = Dict(scope=Scope.user_state)
    # Dictionary mapping cell ids to Boolean values indicating whether the cell was answered
    # correctly at the last check.
    answers_correct = Dict(scope=Scope.user_state, default=None)
    # The number of points awarded.
    score = Float(scope=Scope.user_state)
    # The number of attempts used.
    attempts = Integer(scope=Scope.user_state, default=0)
    # New rows added by the user
    additional_rows = List(scope=Scope.user_state, default=list())

    has_score = True

    @property
    def num_correct_answers(self):
        """The number of correct answers during the last check."""
        if self.answers_correct is None:
            return None
        return sum(self.answers_correct.itervalues())

    @property
    def num_total_answers(self):
        """The total number of answers during the last check."""
        if self.answers_correct is None:
            return None
        return len(self.answers_correct)

    def parse_fields(self):
        """Parse the user-provided fields into more processing-friendly structured data."""
        if self.content:
            self.thead, self.tbody = parse_table(self.content)
            if self.extendable:
                self.tbody.extend(self.process_additional_rows())
        else:
            self.thead = self.tbody = None
            return
        if self.column_widths:
            self._column_widths = parse_number_list(self.column_widths)
        else:
            self._column_widths = [800 / len(self.thead)] * len(self.thead)
        if self.row_heights:
            self._row_heights = parse_number_list(self.row_heights)
        else:
            self._row_heights = [36] * (len(self.tbody) + 1)

    def postprocess_table(self):
        """Augment the parsed table definition with further information.

        The additional information is taken from other content and student state fields.
        """
        self.response_cells = {}
        for row, height in zip(self.tbody, self._row_heights[1:]):
            row['height'] = height
            if row['index'] % 2:
                row['class'] = 'even'
            else:
                row['class'] = 'odd'
            for cell, cell.col_label in zip(row['cells'], self.thead):
                cell.id = 'cell_{}_{}'.format(row['index'], cell.index)
                cell.classes = ''
                if not cell.is_static:
                    self.response_cells[cell.id] = cell
                    cell.classes = 'active'
                    cell.value = self.answers.get(cell.id)
                    cell.height = height - 2
                    if isinstance(cell, NumericCell) and cell.abs_tolerance is None:
                        cell.set_tolerance(self.default_tolerance)

    def get_status(self):
        """Status dictionary passed to the frontend code."""
        return dict(
            answers_correct=self.answers_correct,
            num_correct_answers=self.num_correct_answers,
            num_total_answers=self.num_total_answers,
            score=self.score,
            maximum_score=self.maximum_score,
            attempts=self.attempts,
            max_attempts=self.max_attempts,
            answers=self.answers,
        )

    def student_view(self, context=None):
        """Render the table."""
        self.remove_unsaved_rows()
        self.parse_fields()
        self.postprocess_table()
        headers_style = re.sub('<[^<]*table>', '', self.headers_style) if self.custom_headers else None

        context = dict(
            help_text=self.help_text,
            total_width=sum(self._column_widths) if self._column_widths else None,
            column_widths=self._column_widths,
            head_height=self._row_heights[0] if self._row_heights else None,
            thead=self.thead,
            tbody=self.tbody,
            max_attempts=self.max_attempts,
            score_type=self.score_type,
            extendable=self.extendable,
            headers_style=headers_style,
        )
        html = loader.render_template('templates/html/activetable.html', context)

        css_context = dict(
            correct_icon=self.runtime.local_resource_url(self, 'public/img/correct-icon.png'),
            incorrect_icon=self.runtime.local_resource_url(self, 'public/img/incorrect-icon.png'),
            unanswered_icon=self.runtime.local_resource_url(self, 'public/img/unanswered-icon.png'),
        )
        css = loader.render_template('templates/css/activetable.css', css_context)

        frag = Fragment(html)
        frag.add_css(css)
        frag.add_javascript(loader.load_unicode('static/js/src/activetable.js'))
        frag.initialize_js('ActiveTableXBlock', self.get_status())
        return frag

    def check_and_save_answers(self, data):
        """Common implementation for the check and save handlers."""
        if self.max_attempts and self.attempts >= self.max_attempts:
            # The "Check" button is hidden when the maximum number of attempts has been reached, so
            # we can only get here by manually crafted requests.  We simply return the current
            # status without rechecking or storing the answers in that case.
            return self.get_status()
        self.parse_fields()
        self.postprocess_table()
        if self.extendable:
            self.save_filled_additional_rows(data)
        answers_correct = self.check_responses(data)
        # Since the previous statement executed without error, the data is well-formed enough to be
        # stored.  We now know it's a dictionary and all the keys are valid cell ids.
        self.answers = data
        return answers_correct

    @XBlock.json_handler
    def check_answers(self, data, unused_suffix=''):
        """Check the answers given by the student.

        This handler is called when the "Check" button is clicked.
        """
        self.answers_correct = self.check_and_save_answers(data)
        self.attempts += 1
        self.score = self.num_correct_answers * self.maximum_score / len(self.answers_correct)
        self.runtime.publish(self, 'grade', dict(value=self.score, max_value=self.maximum_score))
        student_item_dict = self.get_student_item_dict()
        api.create_submission(student_item_dict, self.get_status())
        return self.get_status()

    @XBlock.json_handler
    def save_answers(self, data, unused_suffix=''):
        """Save the answers given by the student without checking them."""
        self.answers_correct = self.check_and_save_answers(data)

        if self.score_type == SCORE_OPTIONS[1]:
            self.score = self.num_correct_answers * self.maximum_score / len(self.answers_correct)
            self.runtime.publish(self, 'grade', dict(value=self.score, max_value=self.maximum_score))
        elif self.score_type == SCORE_OPTIONS[2]:
            self.score = self.num_correct_answers * self.maximum_score / self.tbody[-1].get("index")
            self.runtime.publish(self, 'grade', dict(value=self.score, max_value=self.maximum_score))
        else:
            self.answers_correct = None

        student_item_dict = self.get_student_item_dict()
        api.create_submission(student_item_dict, self.get_status())
        return self.get_status()

    @XBlock.json_handler
    def add_row(self, data, unused_suffix=''):
        """Add an additional row to the bottom"""
        self._add_row()
        return self.get_status()

    @XBlock.handler
    def download_pdf_file(self, data, unused_suffix=''):
        """
        Retrieve pdf file containing all table data.
        """
        title = data.params.get("unitTitle")
        MARGIN_VALUE = 30
        TABLE_WIDTH = A4[1] - (2 * MARGIN_VALUE)
        all_element_cells = []

        # We need to parse the table before to parse it to pdf data.
        self.parse_fields()
        data = parse_to_pdf(self.tbody, self.thead, self.answers)
        # Create a file-like buffer to receive PDF data.
        buffer_data = io.BytesIO()

        try:
            # Create the PDF object, using the buffer as its "file."
            document_obj = SimpleDocTemplate(
                buffer_data,
                pagesize=A4,
                rightMargin=MARGIN_VALUE,
                leftMargin=MARGIN_VALUE,
                topMargin=MARGIN_VALUE,
                bottomMargin=MARGIN_VALUE,
            )
            document_obj.pagesize = landscape(A4)
            table_style = TableStyle([
                ('INNERGRID', (0, 0), (-1, -1), 0.25, colors.black),
                ('BOX', (0, 0), (-1, -1), 0.25, colors.black),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ])

            all_element_cells.append(self.generate_pdf_title(title))

            final_data = list(self.generate_pdf_data(data))

            if self.custom_headers:
                all_element_cells.append(self.generate_table_pdf_headers(TABLE_WIDTH))
                final_data.pop(0)

            table_obj = Table(final_data, colWidths=TABLE_WIDTH / len(final_data[0]))
            table_obj.setStyle(table_style)

            # Send the data and build the file
            all_element_cells.append(table_obj)
            document_obj.build(all_element_cells)
        except Exception as exp:
            raise GeneratePdfError('PDF file could not be generated. {}'.format(str(exp)))

        response_obj = Response(content_type='text/plain')
        response_obj.body = buffer_data.getvalue()
        return response_obj

    def validate_field_data(self, validation, data):
        """Validate the data entered by the user.

        This handler is called when the "Save" button is clicked in Studio after editing the
        properties of this XBlock.
        """
        def add_error(msg):
            """Add a validation error."""
            validation.add(ValidationMessage(ValidationMessage.ERROR, msg))
        try:
            thead, tbody = parse_table(data.content)
        except ParseError as exc:
            add_error('Problem with table definition: ' + exc.message)
            thead = tbody = None
        if data.column_widths:
            try:
                column_widths = parse_number_list(data.column_widths)
            except ParseError as exc:
                add_error('Problem with column widths: ' + exc.message)
            else:
                if thead is not None and len(column_widths) != len(thead):
                    add_error(
                        'The number of list entries in the Column widths field must match the '
                        'number of columns in the table.'
                    )
        if data.row_heights:
            try:
                row_heights = parse_number_list(data.row_heights)
            except ParseError as exc:
                add_error('Problem with row heights: ' + exc.message)
            else:
                if tbody is not None and len(row_heights) != len(tbody) + 1:
                    add_error(
                        'The number of list entries in the Row heights field must match the number '
                        'of rows in the table.'
                    )

        if data.custom_headers:
            if not data.headers_style.count("table") == 2:
                add_error('Headers must be defined inside a unique table')

            elif not data.headers_style.count("thead") == 2:
                add_error('Headers must be defined inside a unique <thead></thead> label')

    def _add_row(self):

        self.parse_fields()
        last_item = self.tbody[-1]
        index = last_item.get("index")
        cells = last_item.get("cells", [])

        if not len(cells):
            return
        try:
            previous_column_value = cells[0].value
        except AttributeError:
            previous_column_value = index

        if isinstance(previous_column_value, (int, long)):
            new_column_value = previous_column_value + 1
        else:
            new_column_value = len(self.body) + 1

        self.additional_rows.append({"index": index + 1, "new_column_value": new_column_value, "save": False})

    def process_additional_rows(self):

        last_item = self.tbody[-1]
        cells = last_item.get("cells", [])
        user_rows = list()

        for row in self.additional_rows:
            new_cells = list()
            for idx, item in enumerate(cells):
                if not idx:
                    cell = StaticCell(row.get("new_column_value"))
                else:
                    cell = TextCell("")
                cell.index = idx
                new_cells.append(cell)
            user_rows.append(dict(index=row.get("index"), cells=new_cells))

        return user_rows

    def remove_unsaved_rows(self):
        auxiliar_list = list(self.additional_rows)

        for row in auxiliar_list:
            if not row.get("save"):
                self.additional_rows.remove(row)

    def save_filled_additional_rows(self, data):
        for cell_id, value in data.iteritems():
            index_column = self.response_cells[cell_id].index
            index_row = cell_id[len("cell_"): -len("_{}".format(index_column))]
            if value != "":
                for row in self.additional_rows:
                    if row.get("index") == int(index_row):
                        row["save"] = True
                        break

    def check_responses(self, data):

        if self.score_type == SCORE_OPTIONS[1]:
            return {
                cell_id: True if value != "" else False
                for cell_id, value in data.iteritems()
            }
        elif self.score_type == SCORE_OPTIONS[2]:
            return {
                cell_id: value for cell_id, value in data.iteritems()
            }
        else:
            return {
                cell_id: self.response_cells[cell_id].check_response(value)
                for cell_id, value in data.iteritems()
            }

    def generate_pdf_data(self, data):
        """
        This returns a generator, every element is a list.
        """
        sample_style_sheet = self.generate_style_sheet("BodyText")

        if self.score_type == SCORE_OPTIONS[2]:
            for row in data:
                cell_data = []
                for cell in row:
                    if isinstance(cell, bool) and cell:
                        cell_data.append(Paragraph("X", sample_style_sheet))
                    elif isinstance(cell, bool):
                        cell_data.append(Paragraph("", sample_style_sheet))
                    else:
                        cell_data.append(Paragraph(cell, sample_style_sheet))
                yield(cell_data)
        else:
            for row in data:
                yield([Paragraph(cell, sample_style_sheet) for cell in row])

    def generate_pdf_title(self, title):
        """
        Returns a title from the unit name and component name
        """
        return Paragraph("{}-{}".format(title, self.display_name), self.generate_style_sheet("Heading2"))

    def generate_table_pdf_headers(self, width):
        """
        This returns a Table from the custom html headers.
        """
        data = parse_headers_to_list(self.headers_style)

        def generate_span(cell, row, col, max_row, max_col):
            source = (col, row)
            colspan = cell.get("colspan")
            rowspan = cell.get("rowspan")
            if colspan > 0 and rowspan > 0:
                col += colspan
                row += rowspan
            elif colspan > 0:
                col += colspan
            elif rowspan > 0:
                row += rowspan

            if col > max_col:
                col = max_col
            if row > max_row:
                row = max_row

            return ("SPAN", source, (col, row))

        table_style = [
            ('INNERGRID', (0, 0), (-1, -1), 0.25, colors.black),
            ('BOX', (0, 0), (-1, -1), 0.25, colors.black),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]

        table = []

        for row, element in enumerate(data):
            table_row = []
            for col, cell in enumerate(element):
                if cell.get("value") != "":
                    table_style.append(generate_span(cell, row, col, len(data) - 1, len(element) - 1))
                table_row.append(Paragraph(cell.get("value"), self.generate_style_sheet("BodyText")))
            table.append(table_row)

        return Table(table, style=table_style, colWidths=width / len(table[0]))

    def generate_style_sheet(self, stylesheet):
        """
        This returns a style variable to set cell
        """
        sample_style_sheet = getSampleStyleSheet()
        sample_style_sheet = sample_style_sheet[stylesheet]
        sample_style_sheet.wordWrap = 'CJK'
        sample_style_sheet.alignment = 1
        return sample_style_sheet

    def get_student_item_dict(self, student=None):
        """
        Returns dict required by the submissions app for creating and
        retrieving submissions for a particular student.
        """
        if student is None:
            student = self.runtime.get_real_user(self.runtime.anonymous_student_id)

        return {
            "student_id": student.id,
            "course_id": unicode(self.course_id),
            "item_id": unicode(self.scope_ids.usage_id),
            "item_type": ITEM_TYPE,
        }

    @staticmethod
    def custom_report_format(*args, **kwargs):
        """
        This returns a html string with the activatable answers for the given user and block.
        **Required Parameters:
            student: Instance of django user <User: audit>
            block: Instance of <class 'xblock.internal.ActiveTableXBlockWithMixins'>
        **returns
            String:
                <table>
                <thead>
                    <tr>
                        <th scope="col" style="border: 1px solid #c0c0c0;
                                padding: 10px 10px 5px 10px;
                                vertical-align: middle;">Column header 1</th>

                        <th scope="col" style="border: 1px solid #c0c0c0;
                                padding: 10px 10px 5px 10px;
                                vertical-align: middle;">Column header 2</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td scope="col" style="border: 1px solid #c0c0c0;
                                padding: 10px 10px 5px 10px;
                                vertical-align: middle;">Enter "answer" here:</td>

                        <td scope="col" style="border: 1px solid #c0c0c0;
                                padding: 10px 10px 5px 10px;
                                vertical-align: middle;">An answer</td>
                    </tr>
                    <tr>
                        <td scope="col" style="border: 1px solid #c0c0c0;
                                padding: 10px 10px 5px 10px;
                                vertical-align: middle;">42</td>

                        <td scope="col" style="border: 1px solid #c0c0c0;
                                padding: 10px 10px 5px 10px;
                                vertical-align: middle;">A number</td>
                    </tr>
                </tbody>
                </table>
        """
        student = kwargs.get("student")
        block = kwargs.get("block")

        answers = {}

        if not block:
            return ''
        elif student:
            student_item_dict = block.get_student_item_dict(student=student)
            submission = api.get_submissions(student_item_dict, limit=1)
            try:
                user_answers = submission[0]["answer"]
                answers = user_answers.get("answers", {})
            except IndexError:
                pass

        block.answers = answers
        block.parse_fields()
        block.postprocess_table()
        td = """
            <td
                scope="col"
                style=
                    "border: 1px solid #c0c0c0;
                    padding: 10px 10px 5px 10px;
                    vertical-align: middle;"
            >{}</td>
        """
        th = td.replace("td", "th")
        headers_style = re.sub("<[^<]*table>", "", block.headers_style) if block.custom_headers else None
        header_cells = [th.format(cell) for cell in block.thead]
        header = headers_style if headers_style else "<thead><tr>{}</tr></thead>".format(' '.join(header_cells))
        rows = []
        for row in block.tbody:
            cells = [td.format(cell.value) for cell in row.get("cells")]
            rows.append("<tr>{}</tr>".format("".join(cells)))
        body = "<tbody>{}</tbody>".format(''.join(rows))
        table = "<table>{}{}</table>".format(header, body)
        return table.replace('\n', "")

    @staticmethod
    def workbench_scenarios():
        """A canned scenario for display in the workbench."""
        return [
            ("ActiveTableXBlock",
             """<vertical_demo>
                  <activetable url_name="basic">
                    [
                      ['Event', 'Year'],
                      ['French Revolution', Numeric(answer=1789)],
                      ['Krakatoa volcano explosion', Numeric(answer=1883)],
                      ["Proof of Fermat's last theorem", Numeric(answer=1994)],
                    ]
                  </activetable>
                </vertical_demo>
             """),
        ]


class GeneratePdfError(Exception):
    """The pdf file could not be generated."""
