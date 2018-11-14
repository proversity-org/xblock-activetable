# -*- coding: utf-8 -*-
"""An XBlock with a tabular problem type that requires students to fill in some cells."""
from __future__ import absolute_import, division, unicode_literals

import io
import textwrap

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle
from webob.response import Response
from xblock.core import XBlock
from xblock.fields import Dict, Float, Integer, Scope, String, Boolean, List
from xblock.fragment import Fragment
from xblock.validation import ValidationMessage
from xblockutils.resources import ResourceLoader
from xblockutils.studio_editable import StudioEditableXBlockMixin

from .cells import NumericCell, StaticCell, TextCell
from .parsers import ParseError, parse_table, parse_number_list, parse_to_pdf

loader = ResourceLoader(__name__)  # pylint: disable=invalid-name
SCORE_OPTIONS = ["scorable", "no_right_answer", "unique_option"]


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

    editable_fields = [
        'display_name',
        'content',
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
        )

    def student_view(self, context=None):
        """Render the table."""
        self.remove_unsaved_rows()
        self.parse_fields()
        self.postprocess_table()

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
        MARGIN_VALUE = 30
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
                bottomMargin=MARGIN_VALUE
            )
            document_obj.pagesize = landscape(A4)
            table_style = TableStyle([
                ('INNERGRID', (0, 0), (-1, -1), 0.25, colors.black),
                ('BOX', (0, 0), (-1, -1), 0.25, colors.black),
            ])

            #Configure style and word wrap
            sample_style_sheet = getSampleStyleSheet()
            sample_style_sheet = sample_style_sheet["BodyText"]
            # Set the word wrap at the end of the cell border.
            sample_style_sheet.wordWrap = 'CJK'
            final_data = [[Paragraph(cell, sample_style_sheet) for cell in row] for row in data]
            table_obj = Table(final_data)
            table_obj.setStyle(table_style)

            #Send the data and build the file
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
