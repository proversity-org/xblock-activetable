/* Javascript for ActiveTableXBlock. */
function ActiveTableXBlock(runtime, element, init_args) {

    var checkHandlerUrl = runtime.handlerUrl(element, 'check_answers');
    var saveHandlerUrl = runtime.handlerUrl(element, 'save_answers');
    var addRowHandlerUrl = runtime.handlerUrl(element, 'add_row');
    var downloadHandlerUrl = runtime.handlerUrl(element, 'download_pdf_file');

    function markResponseCells(data) {
        if (data.answers_correct) {
            $.each(data.answers_correct, function(cell_id, correct) {
                var $cell = $('#' + cell_id, element);
                $cell.removeClass('right-answer wrong-answer unchecked');
                if (correct) {
                    $cell.addClass('right-answer');
                    $cell.prop('title', 'correct');
                } else {
                    $cell.addClass('wrong-answer');
                    $cell.prop('title', 'incorrect');
                }
            });
        } else {
            $('td.active', element).removeClass('right-answer wrong-answer').addClass('unchecked');
        }
    }

    function updateStatusMessage(data) {
        var $status = $('.status', element);
        var $status_message = $('.status-message', element);
        if (!data.answers_correct) {
            $status.removeClass('incorrect correct');
            $status.text('unanswered');
            $status_message.text('');
        }
        else if (data.num_total_answers == data.num_correct_answers) {
            $status.removeClass('incorrect').addClass('correct');
            $status.text('correct');
            $status_message.text('Great job!');
        } else {
            $status.removeClass('correct').addClass('incorrect');
            $status.text('incorrect');
            $status_message.text(
                'You have ' + data.num_correct_answers + ' out of ' + data.num_total_answers +
                ' cells correct.'
            );
        }
    }

    function updateFeedback(data) {
        var feedback_msg;
        if (data.score === null) {
            feedback_msg = '(' + data.maximum_score + ' points possible)';
        } else {
            feedback_msg = '(' + data.score + '/' + data.maximum_score + ' points)';
        }
        if (data.max_attempts) {
            feedback_msg = 'You have used ' + data.attempts + ' of ' + data.max_attempts +
                ' submissions ' + feedback_msg;
            if (data.attempts == data.max_attempts - 1) {
                $('.action .check .check-label', element).text('Final check');
            }
            else if (data.attempts >= data.max_attempts) {
                $('.action .check, .action .save', element).hide();
            }
        }
        $('.submission-feedback', element).text(feedback_msg);
    }

    function updateStatus(data) {
        markResponseCells(data);
        updateStatusMessage(data);
        updateFeedback(data);
    }

    function callHandler(url) {
        var answers = {};
        $('td.active', element).each(function() {
            answers[this.id] = $('input', this).val();
        });
        $.ajax({
            type: "POST",
            url: url,
            data: JSON.stringify(answers),
            success: updateStatus,
        });
    }

    function downloadPDFFile(data) {
        var file = new Blob([data], { type: 'application/pdf' });
        var fileURL = URL.createObjectURL(file);
        var link = document.createElement('a');
        link.href = fileURL
        link.download = 'activetable_report.pdf';
        document.body.appendChild(link);
        link.click();
    }

    function downloadPDFHandler() {
        $.ajax({
            type: "GET",
            url: downloadHandlerUrl,
            dataType: "text",
            success: downloadPDFFile,
        });
    }

    function toggleHelp(e) {
        var $help_text = $('#activetable-help-text', element), visible;
        $help_text.toggle();
        visible = $help_text.is(':visible');
        $(this).text(visible ? '-help' : '+help');
        $(this).attr('aria-expanded', visible);
    }

    function addNewRow() {
        var lastRow = $("#activetable tbody").find("tr:last");
        var index = $("#activetable tbody").children("tr").length;
        var newRow = lastRow.clone();
        newRow.toggleClass("odd even");
        newRow.children('td').each(function () {
            var oldId = this.id;
            $(this).replaceWith(createCell(oldId, index));
        });
        lastRow.after(newRow);
    }

    function createCell(cellId, index){
        var newId = cellId.replace("_"+index+"_", "_"+(index+1)+"_")
        var cell = $("<td>").attr("id", newId);

        if (!cellId.endsWith("_0")){
            cell.addClass("active unchecked");
            var cellInput = $("<input>").attr("placeholder", "text response").attr("id", "input_"+newId).attr("type", "text");
            var cellLabel = $("<label>").addClass("sr").attr("for", "input_"+newId);
            cell.append(cellLabel).append(cellInput);
        } else {
            var previousColumnValue = parseInt($("#"+cellId).text());
            if (isNaN(previousColumnValue)){
                cell.text(index+1);
            } else {
                cell.text(previousColumnValue+1);
            }
        }

        return cell;
    }

    $('#activetable-help-button', element).click(toggleHelp);
    $('.action .check', element).click(function (e) { callHandler(checkHandlerUrl); });
    $('.action .save', element).click(function (e) { callHandler(saveHandlerUrl); });
    $('.action .extendable', element).click(function (e) {
        callHandler(addRowHandlerUrl);
        addNewRow(addRowHandlerUrl);
    });
    $('.action .download', element).click(function (e) { downloadPDFHandler(); });
    updateStatus(init_args);
}
