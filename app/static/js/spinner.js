document.getElementById('loadingSpinner').style.display = 'block'; // Show spinner

document.getElementById('transcribe_form').addEventListener('submit', function(event) {
    event.preventDefault(); // Prevent form submission

    const formData = new FormData(this); // Create FormData object from form

    // Send request to server
    fetch('/transcribe', {
        method: 'POST',
        body: formData,
    }).then(response => {
        // Hide spinner on response
        document.getElementById('loadingSpinner').style.display = 'none';
        // Check if response is successful
        if (response.ok) {
            // Download response text
            response.text().then(text => {
                // Create a temporary anchor element
                const downloadLink = document.createElement('a');
                downloadLink.href = 'data:text/plain;charset=utf-8,' + encodeURIComponent(text);
                downloadLink.download = 'downloaded_text.txt';
                // Append anchor to body and click it to trigger download
                document.body.appendChild(downloadLink);
                downloadLink.click();
                // Clean up
                document.body.removeChild(downloadLink);
            });
        } else {
            console.error('Error:', response.status);
        }
    }).catch(error => {
        console.error('Error:', error);
        // Hide spinner on error
        document.getElementById('loadingSpinner').style.display = 'none';
    })});
